from fastapi import FastAPI, Request, Query
from fastapi.responses import Response, HTMLResponse
import logging
import os
from typing import List, Dict, Optional

from pydantic import BaseModel
from sqlalchemy import text

from database.models import Base
from database.session import engine, SessionLocal
from database import crud
from pdf_utils import generate_giftcard_pdf
from email_utils import send_giftcard_email, send_email
import requests

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("giftcard-webhook")

# ID produktu karty podarunkowej w Idosell
GIFT_PRODUCT_ID = 14409

# mapowanie wariantów (sizePanelName) -> wartości nominalnej karty
SIZE_TO_VALUE = {
    "100 zł": 100,
    "200 zł": 200,
    "300 zł": 300,
    "500 zł": 500,
}

class AdminAddCodesPayload(BaseModel):
    value: int
    codes: List[str]

# Globalny klient Idosell – inicjalizowany przy starcie aplikacji
idosell_client: IdosellClient | None = None


@app.on_event("startup")
def on_startup():
    """
    - Tworzy tabele w bazie (gift_codes itd.)
    - Inicjalizuje klienta Idosell (jeśli są zmienne środowiskowe)
    """
    global idosell_client

    Base.metadata.create_all(bind=engine)

    try:
        idosell_client = IdosellClient()
        logger.info("IdosellClient zainicjalizowany poprawnie.")
    except RuntimeError as e:
        # Jeśli nie ma zmiennych środowiskowych – po prostu logujemy info
        logger.warning(
            "IdosellClient nie został zainicjalizowany: %s "
            "(orderNote nie będzie aktualizowane w Idosell).",
            e,
        )


@app.get("/")
def root():
    return {"message": "GiftCard backend działa!"}


# -------------------------------------------------
#   Prosty endpoint do sprawdzania tabel w bazie
# -------------------------------------------------
@app.get("/debug/tables")
def debug_tables():
    db = SessionLocal()
    try:
        result = db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public';"
        )
        tables = [row[0] for row in result]
    finally:
        db.close()
    return tables


# -------------------------------------------------
#   Test generowania PDF z kartą podarunkową
# -------------------------------------------------
@app.get("/debug/test-pdf")
def debug_test_pdf():
    test_code = "TEST-1234-ABCD"
    test_value = 300

    pdf_bytes = generate_giftcard_pdf(code=test_code, value=test_value)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="test-giftcard.pdf"'},
    )


def notify_idosell_about_codes(order_serial: int, codes: List[Dict[str, str]]):
    """
    Aktualizuje notatkę zamówienia (orderNote) w Idosell,
    dopisując informacje o przydzielonych kodach kart.

    Jeśli klient Idosell nie jest skonfigurowany – loguje i wychodzi.
    """
    if not idosell_client:
        logger.info(
            "IdosellClient nie skonfigurowany – pomijam aktualizację orderNote "
            "(order_serial=%s).",
            order_serial,
        )
        return

    try:
        idosell_client.append_order_note_with_vouchers(
            order_serial_number=order_serial,
            vouchers=codes,
            pdf_url=None,  # docelowo możesz tu wstawić link do zbiorczego PDF
        )
        logger.info(
            "Zapisano kody kart w notatce zamówienia (orderSerialNumber=%s).",
            order_serial,
        )
    except IdosellApiError as e:
        logger.exception(
            "Błąd Idosell API przy aktualizacji notatki zamówienia %s: %s",
            order_serial,
            e,
        )
    except Exception as e:
        logger.exception(
            "Nieoczekiwany błąd przy aktualizacji notatki zamówienia %s: %s",
            order_serial,
            e,
        )


@app.post("/webhook/order")
async def webhook_order(request: Request):
    payload = await request.json()

    # Struktura Idosell: dane są w Results[0]
    if not payload.get("Results"):
        logger.warning("Brak 'Results' w webhooku: %s", payload)
        return {"status": "ignored"}

    order = payload["Results"][0]

    order_id = order.get("orderId")
    order_serial = order.get("orderSerialNumber")

    client_email = (
        order.get("clientResult", {})
        .get("clientAccount", {})
        .get("clientEmail")
    )

    order_details = order.get("orderDetails", {})
    products = order_details.get("productsResults", [])
    prepaids = order_details.get("prepaids", [])

    # -------------------------------
    #   1. Sprawdzamy, czy opłacone
    # -------------------------------
    is_paid = any(p.get("paymentStatus") == "y" for p in prepaids)

    if not is_paid:
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

    # -----------------------------------------
    #   2. Szukamy kart podarunkowych w pozycji
    # -----------------------------------------
    gift_lines: List[Dict[str, object]] = []

    for p in products:
        product_id = p.get("productId")
        raw_quantity = p.get("productQuantity", 1)
        name = p.get("productName")
        size = p.get("sizePanelName")  # np. "100 zł", "200 zł", ...

        # Interesują nas tylko pozycje konkretnego produktu (karta podarunkowa)
        if product_id != GIFT_PRODUCT_ID:
            continue

        # Bezpieczna konwersja ilości na int
        try:
            quantity = int(float(raw_quantity))
        except (TypeError, ValueError):
            logger.warning(
                "Nieprawidłowa ilość produktu (productQuantity=%r) – przyjmuję 1 "
                "(orderId=%s, productId=%s)",
                raw_quantity,
                order_id,
                product_id,
            )
            quantity = 1

        value = SIZE_TO_VALUE.get(size)

        if value is None:
            logger.warning(
                "Znaleziono produkt karty (ID=%s), "
                "ale nieznana wartość sizePanelName=%s",
                product_id,
                size,
            )
            continue

        gift_lines.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "name": name,
                "size": size,
                "value": value,
            }
        )

    if not gift_lines:
        logger.info(
            "Opłacone zamówienie %s nie zawiera kart podarunkowych – ignoruję.",
            order_id,
        )
        return {"status": "no_giftcards", "orderId": order_id}

    logger.info("Zamówienie %s zawiera karty: %s", order_id, gift_lines)

    # --------------------------------------
    # 3. Pobranie kodów z puli i zapis w DB
    # --------------------------------------
    db = SessionLocal()
    assigned_codes: List[Dict[str, str]] = []

    try:
        for line in gift_lines:
            qty = line["quantity"]
            value = line["value"]

            for _ in range(qty):
                code_obj = crud.get_free_code(db, value)

                if not code_obj:
                    logger.error(
                        "Brak wolnych kodów dla wartości %s zł (zamówienie %s)",
                        value,
                        order_id,
                    )
                    continue

                used = crud.mark_code_used(db, code_obj, order_id)
                assigned_codes.append(
                    {
                        "code": used.code,
                        "value": used.value,
                    }
                )
    finally:
        db.close()

    logger.info(
        "Przypisane kody dla zamówienia %s: %s",
        order_id,
        assigned_codes,
    )

    # --------------------------------------
    # 4. Generowanie PDF-ów z kartami
    # --------------------------------------
    pdf_files: List[tuple[str, bytes]] = []
    for c in assigned_codes:
        pdf_bytes = generate_giftcard_pdf(code=c["code"], value=c["value"])
        filename = f"giftcard_{c['value']}zl_{c['code']}.pdf"
        pdf_files.append((filename, pdf_bytes))

    # --------------------------------------
    # 5. Wysyłka maila do klienta
    # --------------------------------------
    if client_email and assigned_codes:
        try:
            send_giftcard_email(
                to_email=client_email,
                order_id=order_id,
                order_serial=str(order_serial),
                codes=assigned_codes,
                pdf_files=pdf_files,
            )
        except Exception as e:
            logger.exception("Błąd przy wysyłaniu e-maila z kartą: %s", e)
    else:
        logger.warning(
            "Brak e-maila klienta lub brak przypisanych kodów dla zamówienia %s – "
            "pomijam wysyłkę maila.",
            order_id,
        )

    # --------------------------------------
    # 6. Aktualizacja notatki w Idosell (orderNote)
    # --------------------------------------
    if assigned_codes and order_serial is not None:
        try:
            notify_idosell_about_codes(int(order_serial), assigned_codes)
        except Exception:
            # Logowanie odbywa się już wewnątrz notify_idosell_about_codes
            pass

    # Odpowiedź webhooka
    return {
        "status": "giftcards_assigned",
        "orderId": order_id,
        "giftLines": gift_lines,
        "assignedCodes": assigned_codes,
    }


# -------------------------------------------------
#   Endpoint health-check
# -------------------------------------------------
@app.get("/health")
def health():
    """
    Prosty healthcheck:
    - status aplikacji
    - baza danych
    - SendGrid (API key obecny?)
    - Idosell (env + inicjalizacja klienta)
    - szablon PDF
    """
    # --- DB ---
    db_status = "ok"
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {e.__class__.__name__}"
    finally:
        try:
            db.close()
        except Exception:
            pass

    # --- SendGrid ---
    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
    sendgrid_status = "configured" if sendgrid_api_key else "missing_api_key"

    # --- Idosell ---
    idosell_domain = os.getenv("IDOSELL_DOMAIN")
    idosell_key = os.getenv("IDOSELL_API_KEY")

    idosell_env_ok = bool(idosell_domain and idosell_key)
    idosell_client_initialized = bool(idosell_client)

    idosell_status = {
        "env_ok": idosell_env_ok,
        "client_initialized": idosell_client_initialized,
        "domain_present": bool(idosell_domain),
        "api_key_present": bool(idosell_key),
    }

    # --- PDF template ---
    pdf_template_status = "found" if os.path.exists(TEMPLATE_PATH) else "missing"

    overall_ok = (
        db_status == "ok"
        and sendgrid_status == "configured"
        and pdf_template_status == "found"
    )

    return {
        "status": "ok" if overall_ok else "degraded",
        "services": {
            "database": db_status,
            "sendgrid": sendgrid_status,
            "idosell": idosell_status,
            "pdf_template": pdf_template_status,
        },
    }


# -------------------------------------------------
#   Testowy endpoint wysyłki e-maila
# -------------------------------------------------
@app.get("/debug/test-email")
async def debug_test_email(to: str = Query(..., description="Adres odbiorcy")):
    """
    Testowy endpoint wysyłki email — wysyła testową kartę w PDF jako załącznik.
    """
    pdf_bytes = generate_giftcard_pdf(code="TEST-1234-ABCD", value=300)
    attachments = [("test-giftcard.pdf", pdf_bytes)]

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
        logger.exception("Błąd przy wysyłaniu testowego maila: %s", e)
        return {"status": "error", "message": str(e)}

@app.post("/admin/api/codes")
def admin_add_codes(payload: AdminAddCodesPayload):
    """
    Dodawanie nowych kodów kart dla danego nominału.
    Oczekuje JSON:
    {
        "value": 100,
        "codes": ["AAA-BBB-CCC", "DDD-EEE-FFF"]
    }
    """
    db = SessionLocal()
    inserted = 0
    skipped: List[str] = []

    try:
        for raw_code in payload.codes:
            code = raw_code.strip()
            if not code:
                continue

            try:
                db.execute(
                    text(
                        "INSERT INTO gift_codes (code, value) "
                        "VALUES (:code, :value)"
                    ),
                    {"code": code, "value": payload.value},
                )
                inserted += 1
            except Exception as e:
                # Duplikaty / błędy – pomijamy i zbieramy do listy
                logging.warning("Nie udało się dodać kodu %s: %s", code, e)
                skipped.append(code)

        db.commit()
    finally:
        db.close()

    return {
        "inserted": inserted,
        "skipped": skipped,
    }

@app.get("/admin/api/codes")
def admin_list_codes(
    value: Optional[int] = Query(None, description="Nominał karty, np. 100"),
    used: Optional[str] = Query(
        "all",
        description="Filtr stanu: all|used|free",
    ),
    search: Optional[str] = Query(
        None,
        description="Fragment kodu, np. 'ABC'",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    Zwraca listę kodów z podstawowymi informacjami do tabeli w panelu.
    """
    db = SessionLocal()
    try:
        conditions = ["1=1"]
        params: Dict[str, object] = {}

        if value is not None:
            conditions.append("value = :value")
            params["value"] = value

        if used == "used":
            conditions.append("order_id IS NOT NULL")
        elif used == "free":
            conditions.append("order_id IS NULL")

        if search:
            conditions.append("code ILIKE :search")
            params["search"] = f"%{search}%"

        where_clause = " AND ".join(conditions)

        sql = text(
            f"""
            SELECT id, code, value, order_id
            FROM gift_codes
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT :limit OFFSET :offset
            """
        )
        params["limit"] = limit
        params["offset"] = offset

        rows = db.execute(sql, params).fetchall()

        items = [
            {
                "id": row.id,
                "code": row.code,
                "value": row.value,
                "order_id": row.order_id,
                "status": "used" if row.order_id else "free",
            }
            for row in rows
        ]
    finally:
        db.close()

    return {"items": items}

@app.get("/admin/api/stats")
def admin_stats():
    """
    Zwraca liczbę wolnych/wykorzystanych kodów dla każdego nominału.
    """
    db = SessionLocal()
    try:
        sql = text(
            """
            SELECT
                value,
                COUNT(*) FILTER (WHERE order_id IS NULL) AS free_count,
                COUNT(*) FILTER (WHERE order_id IS NOT NULL) AS used_count
            FROM gift_codes
            GROUP BY value
            ORDER BY value
            """
        )
        rows = db.execute(sql).fetchall()

        stats = [
            {
                "value": row.value,
                "free": row.free_count,
                "used": row.used_count,
            }
            for row in rows
        ]
    finally:
        db.close()

    return {"stats": stats}

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <title>Wassyl GiftCard – Panel administracyjny</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    :root {
      --bg: #0b1520;
      --card-bg: #111827;
      --card-border: #1f2937;
      --accent: #f97316;
      --accent-soft: rgba(249, 115, 22, 0.15);
      --text: #f9fafb;
      --muted: #9ca3af;
      --danger: #f87171;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: radial-gradient(circle at top, #111827 0, #020617 55%, #000 100%);
      color: var(--text);
      min-height: 100vh;
    }

    .page {
      max-width: 1080px;
      margin: 0 auto;
      padding: 24px 16px 48px;
    }

    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 24px;
    }

    header h1 {
      font-size: 1.6rem;
      margin: 0;
      letter-spacing: 0.04em;
    }

    header .subtitle {
      font-size: 0.85rem;
      color: var(--muted);
      margin-top: 4px;
    }

    .grid {
      display: grid;
      grid-template-columns: 1.1fr 1.4fr;
      gap: 20px;
      margin-bottom: 20px;
    }

    @media (max-width: 900px) {
      .grid {
        grid-template-columns: 1fr;
      }
    }

    .card {
      background: radial-gradient(circle at top left, rgba(148, 163, 184, 0.12), transparent 55%),
                  var(--card-bg);
      border-radius: 18px;
      border: 1px solid var(--card-border);
      padding: 18px 18px 16px;
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.8);
    }

    .card h2 {
      margin: 0 0 4px;
      font-size: 1.05rem;
    }

    .card p.desc {
      margin: 0 0 16px;
      font-size: 0.85rem;
      color: var(--muted);
    }

    .badges {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }

    .badge {
      font-size: 0.75rem;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(31, 41, 55, 0.9);
      color: var(--muted);
    }

    .badge strong {
      color: var(--text);
    }

    label {
      display: block;
      font-size: 0.8rem;
      margin-bottom: 4px;
      color: var(--muted);
    }

    select,
    textarea,
    input[type="text"],
    input[type="number"] {
      width: 100%;
      background: rgba(15, 23, 42, 0.95);
      border-radius: 10px;
      border: 1px solid rgba(55, 65, 81, 0.9);
      color: var(--text);
      padding: 7px 9px;
      font-size: 0.86rem;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
    }

    select:focus,
    textarea:focus,
    input[type="text"]:focus,
    input[type="number"]:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 1px rgba(249, 115, 22, 0.5);
      background: rgba(15, 23, 42, 0.98);
    }

    textarea {
      min-height: 120px;
      resize: vertical;
      font-family: monospace;
      font-size: 0.82rem;
      line-height: 1.4;
    }

    .field {
      margin-bottom: 10px;
    }

    .btn-row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 6px;
    }

    button {
      border: none;
      border-radius: 999px;
      padding: 7px 14px;
      font-size: 0.86rem;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: linear-gradient(135deg, #fb923c, #f97316);
      color: #050816;
      font-weight: 600;
      letter-spacing: 0.03em;
      box-shadow: 0 12px 30px rgba(248, 113, 22, 0.35);
      transition: transform 0.1s, box-shadow 0.1s, filter 0.1s;
    }

    button:hover {
      transform: translateY(-1px);
      filter: brightness(1.04);
      box-shadow: 0 16px 35px rgba(248, 113, 22, 0.45);
    }

    button:active {
      transform: translateY(0);
      box-shadow: 0 8px 18px rgba(248, 113, 22, 0.35);
    }

    button.secondary {
      background: rgba(15, 23, 42, 0.95);
      color: var(--muted);
      box-shadow: 0 0 0 transparent;
      border-radius: 999px;
      padding: 6px 11px;
      border: 1px solid rgba(55, 65, 81, 0.9);
      font-weight: 500;
    }

    button.secondary:hover {
      color: var(--text);
      border-color: rgba(148, 163, 184, 0.9);
    }

    .status {
      font-size: 0.8rem;
      min-height: 1.2em;
      color: var(--muted);
    }

    .status.ok {
      color: #4ade80;
    }

    .status.error {
      color: var(--danger);
    }

    /* Stats */
    .stats-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 0 0 10px;
    }

    .stat-pill {
      border-radius: 999px;
      padding: 6px 11px;
      font-size: 0.82rem;
      background: rgba(15, 23, 42, 0.95);
      border: 1px solid rgba(55, 65, 81, 0.9);
    }

    .stat-pill span.label {
      color: var(--muted);
      margin-right: 4px;
    }

    .stat-pill span.free {
      color: #22c55e;
    }

    .stat-pill span.used {
      color: #f97316;
      margin-left: 6px;
    }

    /* Filters & table */
    .filters {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 10px;
      font-size: 0.82rem;
    }

    .filters .field-inline {
      display: flex;
      flex-direction: column;
      gap: 3px;
      min-width: 120px;
    }

    .filters input[type="text"] {
      max-width: 180px;
    }

    .table-wrapper {
      border-radius: 14px;
      border: 1px solid rgba(31, 41, 55, 0.9);
      overflow: hidden;
      background: rgba(15, 23, 42, 0.9);
      max-height: 420px;
      overflow-y: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }

    thead {
      background: rgba(15, 23, 42, 0.9);
      position: sticky;
      top: 0;
      z-index: 1;
    }

    th,
    td {
      padding: 7px 10px;
      text-align: left;
      border-bottom: 1px solid rgba(31, 41, 55, 0.9);
      white-space: nowrap;
    }

    th {
      font-weight: 500;
      color: var(--muted);
      font-size: 0.78rem;
    }

    td.code {
      font-family: monospace;
      font-size: 0.8rem;
    }

    td.order {
      font-family: monospace;
      font-size: 0.78rem;
      color: var(--muted);
    }

    .chip {
      display: inline-flex;
      align-items: center;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 0.72rem;
      border: 1px solid rgba(55, 65, 81, 0.9);
      background: rgba(15, 23, 42, 0.9);
    }

    .chip.used {
      border-color: rgba(248, 113, 22, 0.6);
      color: #fed7aa;
      background: rgba(248, 113, 22, 0.08);
    }

    .chip.free {
      border-color: rgba(22, 163, 74, 0.7);
      color: #bbf7d0;
      background: rgba(22, 163, 74, 0.1);
    }

    .chip span.dot {
      width: 6px;
      height: 6px;
      border-radius: 999px;
      margin-right: 5px;
      display: inline-block;
    }

    .chip.used span.dot {
      background: #f97316;
    }

    .chip.free span.dot {
      background: #22c55e;
    }

    .table-empty {
      padding: 14px;
      text-align: center;
      font-size: 0.82rem;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <div class="page">
    <header>
      <div>
        <h1>Wassyl GiftCard · Panel</h1>
        <div class="subtitle">
          Dodawanie nowych kodów i podgląd przypisania do zamówień Idosell.
        </div>
      </div>
    </header>

    <div class="grid">
      <!-- LEWA KARTA – DODAWANIE KODÓW -->
      <section class="card">
        <h2>Dodaj nowe kody</h2>
        <p class="desc">
          Wklej listę kodów (po jednym w linii) i przypisz je do wybranego nominału.
        </p>

        <div class="badges">
          <div class="badge"><strong>Uwaga:</strong>&nbsp;duplikaty zostaną pominięte</div>
          <div class="badge">Przykład: <strong>ABC-123-XYZ</strong></div>
        </div>

        <form id="add-form">
          <div class="field">
            <label for="value">Nominał karty</label>
            <select id="value" required>
              <option value="100">100 zł</option>
              <option value="200">200 zł</option>
              <option value="300">300 zł</option>
              <option value="500">500 zł</option>
            </select>
          </div>

          <div class="field">
            <label for="codes">Kody (po jednym w linii)</label>
            <textarea id="codes" placeholder="ABC-123-XYZ
DEF-456-UVW"></textarea>
          </div>

          <div class="btn-row">
            <button type="submit">
              ➕ Dodaj kody
            </button>
            <span id="add-status" class="status"></span>
          </div>
        </form>
      </section>

      <!-- PRAWA KARTA – STATYSTYKI + LISTA KODÓW -->
      <section class="card">
        <h2>Pula kodów</h2>
        <p class="desc">
          Stan puli oraz przypisanie kodów do numerów seryjnych zamówień.
        </p>

        <div id="stats" class="stats-row">
          <!-- statystyki ładowane z /admin/api/stats -->
        </div>

        <div class="filters">
          <div class="field-inline">
            <label for="filter-value">Nominał</label>
            <select id="filter-value">
              <option value="">wszystkie</option>
              <option value="100">100 zł</option>
              <option value="200">200 zł</option>
              <option value="300">300 zł</option>
              <option value="500">500 zł</option>
            </select>
          </div>

          <div class="field-inline">
            <label for="filter-used">Status</label>
            <select id="filter-used">
              <option value="all">wszystkie</option>
              <option value="free">tylko wolne</option>
              <option value="used">tylko wykorzystane</option>
            </select>
          </div>

          <div class="field-inline">
            <label for="filter-search">Szukaj po kodzie</label>
            <input id="filter-search" type="text" placeholder="np. ABC" />
          </div>

          <button id="filter-refresh" class="secondary" type="button">
            Odśwież
          </button>
        </div>

        <div class="table-wrapper" id="table-wrapper">
          <div class="table-empty" id="table-empty">
            Ładuję kody...
          </div>
          <table id="codes-table" style="display:none;">
            <thead>
              <tr>
                <th>ID</th>
                <th>Kod</th>
                <th>Nominał</th>
                <th>Status</th>
                <th>Numer seryjny zamówienia</th>
              </tr>
            </thead>
            <tbody id="codes-tbody">
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <script>
    const API_BASE = "/admin/api";

    async function fetchJSON(url, options) {
      const resp = await fetch(url, options);
      if (!resp.ok) {
        throw new Error("HTTP " + resp.status);
      }
      return resp.json();
    }

    // --- Dodawanie kodów ---
    async function handleAddForm(event) {
      event.preventDefault();
      const valueEl = document.getElementById("value");
      const codesEl = document.getElementById("codes");
      const statusEl = document.getElementById("add-status");

      const value = parseInt(valueEl.value, 10);
      const raw = codesEl.value || "";
      const codes = raw
        .split(/\\r?\\n/)
        .map(c => c.trim())
        .filter(c => c.length > 0);

      if (!codes.length) {
        statusEl.textContent = "Brak kodów do dodania.";
        statusEl.className = "status error";
        return;
      }

      statusEl.textContent = "Dodaję...";
      statusEl.className = "status";

      try {
        const payload = { value, codes };
        const data = await fetchJSON(API_BASE + "/codes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        statusEl.className = "status ok";
        statusEl.textContent =
          "Dodano: " + data.inserted + ", pominięto: " + (data.skipped?.length || 0);

        // odśwież statystyki i tabelę
        loadStats();
        loadCodes();

        // opcjonalnie wyczyść textarea
        // codesEl.value = "";
      } catch (err) {
        console.error(err);
        statusEl.className = "status error";
        statusEl.textContent = "Błąd przy dodawaniu kodów.";
      }
    }

    // --- Statystyki ---
    async function loadStats() {
      const statsEl = document.getElementById("stats");
      statsEl.innerHTML = "";

      try {
        const data = await fetchJSON(API_BASE + "/stats");
        const stats = data.stats || [];

        if (!stats.length) {
          statsEl.innerHTML =
            '<div class="stat-pill"><span class="label">Brak danych w bazie.</span></div>';
          return;
        }

        stats.forEach(s => {
          const pill = document.createElement("div");
          pill.className = "stat-pill";
          pill.innerHTML =
            '<span class="label">' + s.value + ' zł</span>' +
            '<span class="free">wolne: ' + s.free + '</span>' +
            '<span class="used">wykorzystane: ' + s.used + "</span>";
          statsEl.appendChild(pill);
        });
      } catch (err) {
        console.error(err);
        statsEl.innerHTML =
          '<div class="stat-pill"><span class="label">Błąd przy pobieraniu statystyk.</span></div>';
      }
    }

    // --- Tabela kodów ---
    async function loadCodes() {
      const value = document.getElementById("filter-value").value;
      const used = document.getElementById("filter-used").value;
      const search = document.getElementById("filter-search").value.trim();

      const emptyEl = document.getElementById("table-empty");
      const tableEl = document.getElementById("codes-table");
      const tbodyEl = document.getElementById("codes-tbody");

      emptyEl.style.display = "block";
      emptyEl.textContent = "Ładuję kody...";
      tableEl.style.display = "none";
      tbodyEl.innerHTML = "";

      const params = new URLSearchParams();
      if (value) params.set("value", value);
      if (used) params.set("used", used);
      if (search) params.set("search", search);
      params.set("limit", "200");

      try {
        const data = await fetchJSON(API_BASE + "/codes?" + params.toString());
        const items = data.items || [];

        if (!items.length) {
          emptyEl.textContent = "Brak kodów dla wybranych filtrów.";
          tableEl.style.display = "none";
          return;
        }

        items.forEach(item => {
          const tr = document.createElement("tr");

          const tdId = document.createElement("td");
          tdId.textContent = item.id;
          tr.appendChild(tdId);

          const tdCode = document.createElement("td");
          tdCode.className = "code";
          tdCode.textContent = item.code;
          tr.appendChild(tdCode);

          const tdValue = document.createElement("td");
          tdValue.textContent = item.value + " zł";
          tr.appendChild(tdValue);

          const tdStatus = document.createElement("td");
          const chip = document.createElement("span");
          chip.className = "chip " + (item.status === "used" ? "used" : "free");
          chip.innerHTML =
            '<span class="dot"></span>' +
            (item.status === "used" ? "wykorzystany" : "wolny");
          tdStatus.appendChild(chip);
          tr.appendChild(tdStatus);

          const tdOrder = document.createElement("td");
          tdOrder.className = "order";
          tdOrder.textContent = item.order_id || "—";
          tr.appendChild(tdOrder);

          tbodyEl.appendChild(tr);
        });

        emptyEl.style.display = "none";
        tableEl.style.display = "table";
      } catch (err) {
        console.error(err);
        emptyEl.textContent = "Błąd przy pobieraniu listy kodów.";
        tableEl.style.display = "none";
      }
    }

    function initFilters() {
      const refreshBtn = document.getElementById("filter-refresh");
      refreshBtn.addEventListener("click", loadCodes);

      document.getElementById("filter-value").addEventListener("change", loadCodes);
      document.getElementById("filter-used").addEventListener("change", loadCodes);

      const searchInput = document.getElementById("filter-search");
      let searchTimeout = null;
      searchInput.addEventListener("input", () => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(loadCodes, 350);
      });
    }

    document.addEventListener("DOMContentLoaded", () => {
      document
        .getElementById("add-form")
        .addEventListener("submit", handleAddForm);

      initFilters();
      loadStats();
      loadCodes();
    });
  </script>
</body>
</html>
"""

@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    """
    Prosty frontend administracyjny do zarządzania kodami kart.
    """
    return ADMIN_HTML


