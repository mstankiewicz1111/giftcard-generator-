import logging
import os
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import (
    Response,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database.models import Base
from database.session import engine, SessionLocal
from database import crud
from pdf_utils import generate_giftcard_pdf
from email_utils import send_giftcard_email, send_email
from idosell_client import IdosellClient, IdosellApiError

# ------------------------------------------------------------------------------
# Konfiguracja aplikacji i logowania
# ------------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("giftcard-webhook")

app = FastAPI(title="Wassyl GiftCard Webhook")

# ------------------------------------------------------------------------------
# Inicjalizacja bazy danych
# ------------------------------------------------------------------------------

Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Inicjalizacja klienta Idosell
# ------------------------------------------------------------------------------

IDOSSELL_DOMAIN = os.getenv("IDOSELL_DOMAIN")
IDOSSELL_API_KEY = os.getenv("IDOSELL_API_KEY")

idosell_client: Optional[IdosellClient] = None

if IDOSSELL_DOMAIN and IDOSSELL_API_KEY:
    try:
        idosell_client = IdosellClient(domain=IDOSELL_DOMAIN, api_key=IDOSELL_API_KEY)
        logger.info("IdosellClient zainicjalizowany dla domeny %s", IDOSSELL_DOMAIN)
    except Exception as e:
        logger.exception("Błąd przy inicjalizacji IdosellClient: %s", e)
        idosell_client = None
else:
    logger.warning(
        "IdosellClient nie został zainicjalizowany: "
        "Brak IDOSELL_DOMAIN lub IDOSELL_API_KEY w zmiennych środowiskowych "
        "(orderNote nie będzie aktualizowane w Idosell)."
    )

# ------------------------------------------------------------------------------
# Stałe związane z produktem karty podarunkowej
# ------------------------------------------------------------------------------

# ID produktu karty podarunkowej w Idosell
GIFT_PRODUCT_ID = 14409

# Nazwy wariantów / rozmiarów odpowiadające nominałom
GIFT_VARIANT_TO_VALUE = {
    "100 zł": 100,
    "200 zł": 200,
    "300 zł": 300,
    "500 zł": 500,
}

# ------------------------------------------------------------------------------
# Endpointy pomocnicze / debug
# ------------------------------------------------------------------------------


@app.get("/", response_class=PlainTextResponse)
def root():
    return "Wassyl GiftCard backend działa."


@app.get("/health")
def health():
    """
    Prosty healthcheck:
    - sprawdza połączenie z bazą
    - sprawdza konfigurację SendGrid
    - sprawdza obecność szablonu PDF
    - sprawdza konfigurację Idosell
    """
    db_status = "ok"
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
    except Exception as e:
        logger.exception("Healthcheck DB error: %s", e)
        db_status = "error"
    finally:
        try:
            db.close()
        except Exception:
            pass

    from email_utils import SENDGRID_API_KEY as SG_KEY  # import lokalny, żeby nie robić cykli
    sendgrid_status = "configured" if SG_KEY else "missing"

    from pdf_utils import TEMPLATE_PATH  # ścieżka do WASSYL-GIFTCARD.pdf
    pdf_template_status = "found" if os.path.exists(TEMPLATE_PATH) else "missing"

    idosell_status = "configured" if idosell_client is not None else "missing"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "services": {
            "database": db_status,
            "sendgrid": sendgrid_status,
            "pdf_template": pdf_template_status,
            "idosell": idosell_status,
        },
    }


@app.get("/debug/tables")
def debug_tables():
    """
    Zwraca listę tabel w schemacie public – pomocne przy diagnozie bazy.
    """
    db = SessionLocal()
    try:
        result = db.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
        )
        tables = [row[0] for row in result]
    finally:
        db.close()
    return {"tables": tables}


@app.get("/debug/test-pdf")
def debug_test_pdf():
    """
    Generuje testowy PDF z przykładową kartą.
    """
    pdf_bytes = generate_giftcard_pdf(code="TEST-1234-ABCD", value=300)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="test-giftcard.pdf"'},
    )


@app.get("/debug/test-email")
def debug_test_email(to: str = Query(..., description="Adres e-mail odbiorcy testu")):
    """
    Wysyła testowy e-mail z jedną przykładową kartą w załączniku.
    """
    pdf_bytes = generate_giftcard_pdf(code="TEST-1234-ABCD", value=300)

    attachments = [("TEST-GIFTCARD.pdf", pdf_bytes)]

    try:
        send_email(
            to_email=to,
            subject="Test wysyłki z załącznikiem – Wassyl GiftCard",
            body_text=(
                "To jest testowy email wysłany z backendu karty podarunkowej.\n"
                "W załączniku znajdziesz przykładową kartę w PDF."
            ),
            attachments=attachments,
        )
        return {"status": "ok", "message": f"Wysłano testową wiadomość na {to}"}
    except Exception as e:
        logger.exception("Błąd podczas wysyłki testowego maila: %s", e)
        return {"status": "error", "message": str(e)}


# ------------------------------------------------------------------------------
# Webhook z Idosell – obsługa zamówienia
# ------------------------------------------------------------------------------


def _extract_giftcard_positions(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Zwraca listę pozycji z zamówienia, które odpowiadają kartom podarunkowym.
    """
    order_details = order.get("orderDetails") or {}
    products = order_details.get("productsResults") or []

    gift_positions: List[Dict[str, Any]] = []

    for p in products:
        try:
            product_id = p.get("productId")
            size_panel_name = (p.get("sizePanelName") or "").strip()
            quantity = int(p.get("quantity") or 0)
        except Exception:
            continue

        if product_id != GIFT_PRODUCT_ID or quantity <= 0:
            continue

        value = GIFT_VARIANT_TO_VALUE.get(size_panel_name)
        if not value:
            logger.warning(
                "Pozycja z produktem GIFT_PRODUCT_ID, ale nieznany wariant '%s' – pomijam.",
                size_panel_name,
            )
            continue

        gift_positions.append(
            {
                "value": value,
                "quantity": quantity,
                "raw": p,
            }
        )

    return gift_positions


def _is_order_paid(order: Dict[str, Any]) -> bool:
    order_details = order.get("orderDetails") or {}
    prepaids = order_details.get("prepaids") or []
    return any(p.get("paymentStatus") == "y" for p in prepaids)


@app.post("/webhook/order")
async def idosell_order_webhook(request: Request):
    """
    Główny webhook odbierający zamówienia z Idosell.
    """
    payload = await request.json()
    order: Optional[Dict[str, Any]] = payload.get("order")

    if not isinstance(order, dict):
        logger.error("Webhook /webhook/order: brak lub nieprawidłowa sekcja 'order'")
        return JSONResponse({"status": "ignored", "reason": "no_order"}, status_code=400)

    order_id = order.get("orderId")
    order_serial = order.get("orderSerialNumber")
    client_email = (
        (order.get("client") or {})
        .get("clientContactData") or {}
    ).get("clientEmail") or order.get("clientEmail")

    if not _is_order_paid(order):
        logger.info(
            "Zamówienie %s (%s) NIE jest opłacone – przerywam.",
            order_id,
            order_serial,
        )
        return {"status": "not_paid", "orderId": order_id}

    logger.info(
        "Odebrano OPŁACONE zamówienie: orderId=%s, serial=%s, email=%s",
        order_id,
        order_serial,
        client_email,
    )

    # 2. Wyciągamy pozycje z kartami podarunkowymi
    gift_positions = _extract_giftcard_positions(order)

    if not gift_positions:
        logger.info(
            "Opłacone zamówienie %s nie zawiera kart podarunkowych – ignoruję.",
            order_id,
        )
        return {"status": "no_giftcards", "orderId": order_id}

    # 3. Przydzielamy kody z puli
    db = SessionLocal()
    assigned_codes: List[Dict[str, Any]] = []
    try:
        for pos in gift_positions:
            value = pos["value"]
            quantity = pos["quantity"]

            for _ in range(quantity):
                code_obj = crud.assign_unused_gift_code(db, value=value, order_id=order_serial)
                if not code_obj:
                    logger.error(
                        "Brak dostępnych kodów dla nominału %s – przerwano proces zamówienia %s",
                        value,
                        order_id,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Brak dostępnych kodów dla nominału {value}",
                    )

                assigned_codes.append(
                    {
                        "code": code_obj.code,
                        "value": code_obj.value,
                    }
                )

        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Błąd podczas przydzielania kodów dla zamówienia %s (%s)",
            order_id,
            order_serial,
        )
        raise
    finally:
        db.close()

    # 4. Wysyłka e-maila z kartą/kartami
    if client_email and assigned_codes:
        try:
            send_giftcard_email(
                to_email=client_email,
                codes=assigned_codes,
                order_serial_number=str(order_serial),
            )
            logger.info(
                "Wysłano e-mail z kartą/kartami dla zamówienia %s (%s) na adres %s",
                order_id,
                order_serial,
                client_email,
            )
        except Exception as e:
            logger.exception("Błąd przy wysyłaniu e-maila z kartą: %s", e)
    else:
        logger.warning(
            "Brak e-maila klienta lub brak przypisanych kodów dla zamówienia %s – pomijam wysyłkę maila.",
            order_id,
        )

    # 5. Aktualizacja notatki zamówienia w Idosell
    if assigned_codes and order_serial and idosell_client:
        codes_text = ", ".join(
            f"{c['code']} ({c['value']} zł)" for c in assigned_codes
        )
        note_text = f"Numer(y) karty podarunkowej: {codes_text}"

        try:
            idosell_client.update_order_note(order_serial, note_text)
        except IdosellApiError as e:
            logger.error(
                "Błąd IdosellApiError przy aktualizacji notatki zamówienia %s: %s",
                order_serial,
                e,
            )
        except Exception as e:
            logger.exception(
                "Nieoczekiwany błąd przy aktualizacji notatki zamówienia %s: %s",
                order_serial,
                e,
            )
    elif not idosell_client:
        logger.warning(
            "IdosellClient nie jest zainicjalizowany – pomijam aktualizację notatki zamówienia %s",
            order_serial,
        )

    return {
        "status": "processed",
        "orderId": order_id,
        "orderSerialNumber": order_serial,
        "assigned_codes": assigned_codes,
    }


# ------------------------------------------------------------------------------
# PROSTY PANEL ADMINISTRACYJNY / FRONTEND
# ------------------------------------------------------------------------------


ADMIN_HTML = """
<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8" />
  <title>WASSYL – panel kart podarunkowych</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    :root {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f5f7;
      color: #111827;
    }
    body {
      margin: 0;
      padding: 0;
      background: #f5f5f7;
    }
    .page {
      max-width: 1100px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
      gap: 24px;
    }
    @media (max-width: 800px) {
      .grid {
        grid-template-columns: minmax(0, 1fr);
      }
    }
    .card {
      background: #ffffff;
      border-radius: 16px;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.08);
      padding: 20px 20px 18px;
    }
    h1 {
      font-size: 24px;
      margin: 0 0 12px;
    }
    h2 {
      font-size: 18px;
      margin: 0 0 12px;
    }
    .subtle {
      color: #6b7280;
      font-size: 14px;
      margin-bottom: 16px;
    }
    label {
      font-size: 14px;
      font-weight: 500;
      display: block;
      margin-bottom: 4px;
    }
    input, select, textarea {
      width: 100%;
      box-sizing: border-box;
      padding: 8px 10px;
      border-radius: 8px;
      border: 1px solid #d1d5db;
      font-size: 14px;
      outline: none;
    }
    input:focus, select:focus, textarea:focus {
      border-color: #111827;
    }
    textarea {
      min-height: 80px;
      resize: vertical;
    }
    button {
      border-radius: 999px;
      border: none;
      padding: 8px 16px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      background: #111827;
      color: white;
      transition: background 0.15s ease, transform 0.05s ease;
    }
    button:hover {
      background: #000000;
    }
    button:active {
      transform: translateY(1px);
    }
    .btn-secondary {
      background: #e5e7eb;
      color: #111827;
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      align-items: center;
      margin-bottom: 10px;
    }
    .row > * {
      flex: 1 1 0;
    }
    .row .shrink {
      flex: 0 0 auto;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      background: #f3f4f6;
      color: #374151;
    }
    .badge-green {
      background: #ecfdf5;
      color: #166534;
    }
    .badge-gray {
      background: #f3f4f6;
      color: #374151;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      margin-top: 8px;
    }
    th, td {
      text-align: left;
      padding: 6px 4px;
      border-bottom: 1px solid #e5e7eb;
      vertical-align: top;
    }
    th {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      color: #6b7280;
    }
    tr:last-child td {
      border-bottom: none;
    }
    .muted {
      color: #9ca3af;
      font-size: 13px;
    }
    .code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace;
      font-size: 13px;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 500;
    }
    .status-unused {
      background: #ecfdf5;
      color: #166534;
    }
    .status-used {
      background: #fef2f2;
      color: #b91c1c;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 4px;
    }
    .chip {
      border-radius: 999px;
      border: 1px solid #e5e7eb;
      padding: 2px 10px;
      font-size: 12px;
      cursor: default;
      background: #f9fafb;
    }
    .chip strong {
      font-weight: 600;
    }
    .danger {
      color: #b91c1c;
    }
    .mt-12 { margin-top: 12px; }
    .mt-16 { margin-top: 16px; }
    .mt-20 { margin-top: 20px; }
    .text-right { text-align: right; }
  </style>
</head>
<body>
  <div class="page">
    <div style="margin-bottom: 20px;">
      <h1>WASSYL – karty podarunkowe</h1>
      <p class="subtle">
        Panel do zarządzania pulą kodów kart podarunkowych i podglądu wykorzystania.
      </p>
    </div>

    <div class="grid">
      <!-- LEWA KOLUMNA – dodawanie kodów -->
      <div class="card">
        <h2>Dodaj nowe kody</h2>
        <p class="subtle">
          Wklej listę kodów, po jednym w linii, wybierz nominał i zapisz. Kody trafią do puli
          dostępnej dla webhooka po opłaceniu zamówienia.
        </p>

        <div class="mt-12">
          <label for="nominal">Nominał karty</label>
          <select id="nominal">
            <option value="100">100 zł</option>
            <option value="200">200 zł</option>
            <option value="300">300 zł</option>
            <option value="500">500 zł</option>
          </select>
        </div>

        <div class="mt-12">
          <label for="codes">Kody (po jednym w linii)</label>
          <textarea id="codes" placeholder="ABC-123-XYZ
DEF-456-UVW"></textarea>
        </div>

        <div class="mt-16 row">
          <div class="shrink">
            <button id="btn-add-codes">Zapisz kody</button>
          </div>
          <div id="add-status" class="muted"></div>
        </div>
      </div>

      <!-- PRAWA KOLUMNA – statystyki + lista kodów -->
      <div class="card">
        <h2>Aktualna pula kodów</h2>
        <p class="subtle">
          Podsumowanie ilości dostępnych i wykorzystanych kodów według nominału,
          oraz tabela z ostatnimi kodami.
        </p>

        <div id="stats" class="mt-12 muted">Ładowanie statystyk...</div>

        <div class="mt-20">
          <div class="row">
            <div>
              <label for="filter-value">Filtr nominalu</label>
              <select id="filter-value">
                <option value="">Wszystkie nominały</option>
                <option value="100">100 zł</option>
                <option value="200">200 zł</option>
                <option value="300">300 zł</option>
                <option value="500">500 zł</option>
              </select>
            </div>
            <div>
              <label for="filter-used">Status</label>
              <select id="filter-used">
                <option value="">Wszystkie</option>
                <option value="unused">Tylko niewykorzystane</option>
                <option value="used">Tylko wykorzystane</option>
              </select>
            </div>
            <div class="shrink">
              <button class="btn-secondary" id="btn-refresh-table">Odśwież</button>
            </div>
          </div>

          <div class="mt-12">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Kod</th>
                  <th>Nominał</th>
                  <th>Status</th>
                  <th>Numer zamówienia</th>
                </tr>
              </thead>
              <tbody id="codes-tbody">
              </tbody>
            </table>
            <p id="codes-empty" class="muted">Brak danych do wyświetlenia.</p>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    async function fetchStats() {
      const statsEl = document.getElementById("stats");
      statsEl.textContent = "Ładowanie statystyk...";

      try {
        const res = await fetch("/admin/api/stats");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();

        if (!data || !data.items || data.items.length === 0) {
          statsEl.innerHTML = '<span class="muted">Brak danych – dodaj pierwsze kody.</span>';
          return;
        }

        const parts = data.items.map(item => {
          const val = item.value;
          const total = item.total;
          const used = item.used;
          const unused = item.unused;
          return `
            <div class="chips">
              <div class="chip"><strong>${val} zł</strong></div>
              <div class="chip">Razem: ${total}</div>
              <div class="chip">Dostępne: ${unused}</div>
              <div class="chip">Wykorzystane: ${used}</div>
            </div>
          `;
        });

        statsEl.innerHTML = parts.join("");
      } catch (err) {
        console.error(err);
        statsEl.innerHTML = '<span class="danger">Błąd przy pobieraniu statystyk.</span>';
      }
    }

    async function fetchCodes() {
      const tbody = document.getElementById("codes-tbody");
      const emptyEl = document.getElementById("codes-empty");
      tbody.innerHTML = "";
      emptyEl.textContent = "Ładowanie...";

      const valueSel = document.getElementById("filter-value");
      const usedSel = document.getElementById("filter-used");

      const params = new URLSearchParams();
      if (valueSel.value) params.append("value", valueSel.value);
      if (usedSel.value === "unused") params.append("used", "false");
      if (usedSel.value === "used") params.append("used", "true");

      try {
        const res = await fetch("/admin/api/codes?" + params.toString());
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();

        const codes = data.items || [];
        if (codes.length === 0) {
          emptyEl.textContent = "Brak danych do wyświetlenia.";
          return;
        }

        emptyEl.textContent = "";
        const rows = codes.map(code => {
          const status = code.order_id ? "used" : "unused";
          const statusLabel = status === "used" ? "Wykorzystany" : "Dostępny";
          const statusClass = status === "used" ? "status-used" : "status-unused";

          return `
            <tr>
              <td>${code.id}</td>
              <td class="code">${code.code}</td>
              <td>${code.value} zł</td>
              <td><span class="status-pill ${statusClass}">${statusLabel}</span></td>
              <td>${code.order_id || '<span class="muted">–</span>'}</td>
            </tr>
          `;
        });
        tbody.innerHTML = rows.join("");
      } catch (err) {
        console.error(err);
        emptyEl.textContent = "Błąd przy pobieraniu listy kodów.";
      }
    }

    async function addCodes() {
      const nominalSel = document.getElementById("nominal");
      const codesTextarea = document.getElementById("codes");
      const statusEl = document.getElementById("add-status");

      const nominal = nominalSel.value;
      const raw = codesTextarea.value || "";
      const lines = raw
        .split(/\\r?\\n/)
        .map(l => l.trim())
        .filter(l => l.length > 0);

      if (!nominal || lines.length === 0) {
        statusEl.textContent = "Podaj nominał i co najmniej jeden kod.";
        statusEl.classList.add("danger");
        return;
      }

      statusEl.textContent = "Zapisuję...";
      statusEl.classList.remove("danger");

      try {
        const res = await fetch("/admin/api/codes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            value: parseInt(nominal, 10),
            codes: lines,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Błąd zapisu");
        }
        statusEl.textContent = "Zapisano " + lines.length + " kodów.";
        codesTextarea.value = "";
        fetchStats();
        fetchCodes();
      } catch (err) {
        console.error(err);
        statusEl.textContent = "Błąd przy zapisie kodów.";
        statusEl.classList.add("danger");
      }
    }

    document.getElementById("btn-add-codes").addEventListener("click", addCodes);
    document.getElementById("btn-refresh-table").addEventListener("click", function () {
      fetchStats();
      fetchCodes();
    });

    // pierwsze ładowanie
    fetchStats();
    fetchCodes();
  </script>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(content=ADMIN_HTML)


# ------------------------------------------------------------------------------
# API admina – statystyki i lista kodów
# ------------------------------------------------------------------------------


@app.get("/admin/api/stats")
def admin_stats():
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT
                    value,
                    COUNT(*) AS total,
                    COUNT(order_id) AS used,
                    COUNT(*) - COUNT(order_id) AS unused
                FROM gift_codes
                GROUP BY value
                ORDER BY value ASC
                """
            )
        ).fetchall()

        items = [
            {
                "value": int(r.value),
                "total": int(r.total),
                "used": int(r.used),
                "unused": int(r.unused),
            }
            for r in rows
        ]
        return {"items": items}
    except SQLAlchemyError as e:
        logger.exception("Błąd podczas pobierania statystyk kodów: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()


@app.get("/admin/api/codes")
def admin_list_codes(
    value: Optional[int] = Query(None, description="Filtr po nominale"),
    used: Optional[bool] = Query(
        None,
        description="Filtr po statusie: true=wykorzystane, false=niewykorzystane",
    ),
    limit: int = Query(100, ge=1, le=500),
):
    db = SessionLocal()
    try:
        sql = """
            SELECT id, code, value, order_id
            FROM gift_codes
            WHERE 1=1
        """
        params: Dict[str, Any] = {}

        if value is not None:
            sql += " AND value = :value"
            params["value"] = value

        if used is True:
            sql += " AND order_id IS NOT NULL"
        elif used is False:
            sql += " AND order_id IS NULL"

        sql += " ORDER BY id DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = 0

        rows = db.execute(text(sql), params).fetchall()
        items = [
            {
                "id": r.id,
                "code": r.code,
                "value": int(r.value),
                "order_id": r.order_id,
            }
            for r in rows
        ]
        return {"items": items, "total": len(items)}
    except SQLAlchemyError as e:
        logger.exception("Błąd podczas pobierania listy kodów: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()


@app.post("/admin/api/codes")
async def admin_add_codes(payload: Dict[str, Any]):
    """
    Dodawanie nowych kodów do puli.
    JSON body:
    {
      "value": 100,
      "codes": ["ABC-123", "DEF-456", ...]
    }
    """
    value = payload.get("value")
    codes = payload.get("codes")

    if not isinstance(value, int):
        raise HTTPException(status_code=400, detail="Pole 'value' musi być liczbą całkowitą.")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise HTTPException(
            status_code=400,
            detail="Pole 'codes' musi być listą stringów.",
        )

    cleaned_codes = [c.strip() for c in codes if c.strip()]
    if not cleaned_codes:
        raise HTTPException(status_code=400, detail="Brak kodów do zapisania.")

    db = SessionLocal()
    try:
        for code in cleaned_codes:
            db.execute(
                text(
                    """
                    INSERT INTO gift_codes (code, value, order_id)
                    VALUES (:code, :value, NULL)
                    """
                ),
                {"code": code, "value": value},
            )
        db.commit()
        logger.info(
            "Dodano %s nowych kodów dla nominału %s.",
            len(cleaned_codes),
            value,
        )
        return {"status": "ok", "inserted": len(cleaned_codes)}
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Błąd podczas dodawania nowych kodów: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()
