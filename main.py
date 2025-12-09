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

app = FastAPI(title="WASSYL Giftcard Webhook")

# Inicjalizacja bazy
Base.metadata.create_all(bind=engine)

# Globalny klient Idosell (może być None, jeśli brak konfiguracji)
IDOSELL_DOMAIN = os.getenv("IDOSELL_DOMAIN")
IDOSELL_API_KEY = os.getenv("IDOSELL_API_KEY")

if IDOSELL_DOMAIN and IDOSELL_API_KEY:
    idosell_client: Optional[IdosellClient] = IdosellClient(
        domain=IDOSELL_DOMAIN,
        api_key=IDOSELL_API_KEY,
    )
    logger.info("IdosellClient został zainicjalizowany.")
else:
    idosell_client = None
    logger.warning(
        "Brak konfiguracji IDOSELL_DOMAIN lub IDOSELL_API_KEY - "
        "integracja z Idosell będzie nieaktywna."
    )

# Stałe dla produktu karty podarunkowej
GIFT_PRODUCT_ID = 14409
GIFT_VARIANTS = {
    "100 zł": 100,
    "200 zł": 200,
    "300 zł": 300,
    "500 zł": 500,
}


# ------------------------------------------------------------------------------
# Funkcje pomocnicze
# ------------------------------------------------------------------------------


def _extract_giftcard_positions(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Zwraca listę pozycji koszyka, które są kartami podarunkowymi.

    Każdy element ma postać:
    {
      "value": 100,
      "quantity": 2
    }
    """
    result: List[Dict[str, Any]] = []

    order_details = order.get("orderDetails") or {}

    # Idosell w Twoim payloadzie używa 'productsResults'
    products = order_details.get("productsResults") or []
    # gdyby kiedyś pojawiło się 'basket', też je obsłużymy:
    if not products:
        products = order_details.get("basket") or []

    for item in products:
        product_id = item.get("productId")
        if product_id != GIFT_PRODUCT_ID:
            continue

        size_panel_name = item.get("sizePanelName")
        value = GIFT_VARIANTS.get(size_panel_name)
        if not value:
            logger.warning(
                "Pozycja karty podarunkowej z nieznanym wariantem '%s' (productId=%s).",
                size_panel_name,
                product_id,
            )
            continue

        # w productsResults ilość jest w polu 'productQuantity'
        quantity = (
            item.get("productQuantity")
            or item.get("quantity")
            or 1
        )

        result.append({"value": value, "quantity": quantity})

    return result


def _is_order_paid(order: Dict[str, Any]) -> bool:
    """
    Sprawdza, czy zamówienie jest opłacone.
    Zakładamy, że w orderDetails.prepaids[*].paymentStatus == 'y' oznacza opłacone.
    """
    order_details = order.get("orderDetails") or {}
    prepaids = order_details.get("prepaids") or []
    return any(p.get("paymentStatus") == "y" for p in prepaids)


# ------------------------------------------------------------------------------
# Webhook z Idosell
# ------------------------------------------------------------------------------


@app.post("/webhook/order")
async def idosell_order_webhook(request: Request):
    """
    Główny webhook odbierający zamówienia z Idosell.
    """
    payload = await request.json()

    order: Optional[Dict[str, Any]] = None

    # Obsługa różnych możliwych struktur payloadu z Idosell:
    # 1) {"order": {...}}
    # 2) {"orders": [ {...}, ... ]}
    # 3) {"Results": [ {...}, ... ]}
    # 4) płaski obiekt zawierający orderId i orderSerialNumber
    if isinstance(payload, dict):
        if isinstance(payload.get("order"), dict):
            order = payload.get("order")
        elif isinstance(payload.get("orders"), list) and payload["orders"]:
            first = payload["orders"][0]
            if isinstance(first, dict):
                order = first
        elif isinstance(payload.get("Results"), list) and payload["Results"]:
            first = payload["Results"][0]
            if isinstance(first, dict):
                order = first
        elif "orderId" in payload and "orderSerialNumber" in payload:
            order = payload

    if not isinstance(order, dict):
        logger.error(
            "Webhook /webhook/order: brak lub nieprawidłowa sekcja 'order'. Payload: %s",
            payload,
        )
        return JSONResponse(
            {"status": "ignored", "reason": "no_order"},
            status_code=400,
        )

    order_id = order.get("orderId")
    order_serial = order.get("orderSerialNumber")

    # Szukanie maila w kilku możliwych miejscach
    client_email: Optional[str] = None

    # wariant 1: order["client"]["contact"]["email"]
    client = order.get("client") or {}
    contact = client.get("contact") or {}
    if isinstance(contact, dict):
        client_email = contact.get("email")

    # wariant 2: order["clientResult"]["endClientAccount"]["clientEmail"]
    if not client_email:
        client_result = order.get("clientResult") or {}
        end_client = client_result.get("endClientAccount") or {}
        if isinstance(end_client, dict):
            client_email = end_client.get("clientEmail")

    # wariant 3: order["clientResult"]["clientAccount"]["clientEmail"]
        if not client_email:
            client_account = client_result.get("clientAccount") or {}
            if isinstance(client_account, dict):
                client_email = client_account.get("clientEmail")

    logger.info(
        "Odebrano webhook dla zamówienia %s (serial: %s), e-mail klienta: %s",
        order_id,
        order_serial,
        client_email,
    )

    # ...reszta funkcji bez zmian (sprawdzenie opłacenia, przydział kodów itd.)


    # 1. Sprawdzamy, czy zamówienie jest opłacone
    if not _is_order_paid(order):
        logger.info(
            "Zamówienie %s nie jest jeszcze opłacone – ignoruję.",
            order_id,
        )
        return {"status": "not_paid", "orderId": order_id}

    # 2. Wyciągamy pozycje kart podarunkowych
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
        order_serial_str = str(order_serial)

        for pos in gift_positions:
            value = pos["value"]
            quantity = pos["quantity"]  # ile kart tego nominału wynika z koszyka

            # Ile kodów tego nominału już przypisaliśmy temu zamówieniu?
            existing_count = db.execute(
                text(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM gift_codes
                    WHERE order_id = :order_id
                      AND value = :value
                    """
                ),
                {"order_id": order_serial_str, "value": value},
            ).scalar_one()

            remaining = quantity - existing_count

            if remaining <= 0:
                logger.info(
                    "Zamówienie %s (%s): dla nominału %s zł istnieje już %s kodów (wymagane %s) – nie przydzielam nowych.",
                    order_id,
                    order_serial,
                    value,
                    existing_count,
                    quantity,
                )
                continue

            logger.info(
                "Zamówienie %s (%s): dla nominału %s zł potrzebujemy jeszcze %s kod(ów) (łącznie %s, już istnieje %s).",
                order_id,
                order_serial,
                value,
                remaining,
                quantity,
                existing_count,
            )

            for _ in range(remaining):
                code_obj = crud.assign_unused_gift_code(
                    db,
                    value=value,
                    order_id=order_serial_str,
                )
                if not code_obj:
                    logger.error(
                        "Brak dostępnych kodów dla nominału %s – przerwano proces zamówienia %s",
                        value,
                        order_id,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Brak kodów dla nominału {value}",
                    )

                assigned_codes.append(
                    {"code": code_obj.code, "value": code_obj.value}
                )

        db.commit()
        logger.info(
            "Przydzielono %s nowych kodów dla zamówienia %s (%s).",
            len(assigned_codes),
            order_id,
            order_serial,
        )

    except Exception as e:
        db.rollback()
        logger.exception("Błąd podczas przydzielania kodów dla zamówienia %s (%s): %s",
                        order_id, order_serial, e)
        raise

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
            "Brak skonfigurowanego klienta Idosell – pomijam aktualizację notatki dla zamówienia %s.",
            order_id,
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
      color-scheme: light;
    }
    body {
      margin: 0;
      padding: 0;
      background: #f9fafb;
      color: #111827;
    }
    .page {
      max-width: 960px;
      margin: 0 auto;
      padding: 32px 16px 64px;
    }
    .card {
      background: #ffffff;
      border-radius: 20px;
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.07);
      border: 1px solid #e5e7eb;
      padding: 24px 24px 28px;
      margin-bottom: 24px;
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 12px;
    }
    .card-title {
      font-size: 18px;
      font-weight: 600;
      letter-spacing: -0.01em;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .card-title-badge {
      font-size: 11px;
      font-weight: 500;
      border-radius: 999px;
      padding: 3px 9px;
      border: 1px solid #e5e7eb;
      color: #6b7280;
      background: #f9fafb;
      text-transform: uppercase;
      letter-spacing: 0.09em;
    }
    .card-description {
      font-size: 13px;
      color: #6b7280;
    }
    .section-label {
      text-transform: uppercase;
      font-size: 11px;
      letter-spacing: 0.12em;
      color: #9ca3af;
      margin-bottom: 8px;
      font-weight: 500;
    }
    .muted {
      font-size: 13px;
      color: #6b7280;
    }
    .input, select, textarea {
      width: 100%;
      border-radius: 10px;
      border: 1px solid #d1d5db;
      padding: 8px 10px;
      font-size: 13px;
      outline: none;
      background: #f9fafb;
      transition: border-color 120ms, box-shadow 120ms, background 120ms;
    }
    .input:focus, select:focus, textarea:focus {
      border-color: #6366f1;
      box-shadow: 0 0 0 1px rgba(79, 70, 229, 0.25);
      background: #ffffff;
    }
    textarea {
      resize: vertical;
      min-height: 80px;
    }
    .btn {
      border-radius: 999px;
      padding: 8px 14px;
      font-size: 13px;
      border: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-weight: 500;
      background: #111827;
      color: #f9fafb;
      box-shadow: 0 14px 30px rgba(15, 23, 42, 0.3);
      transition: transform 120ms, box-shadow 120ms, background 120ms, opacity 120ms;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.35);
      background: #020617;
    }
    .btn:active {
      transform: translateY(0);
      box-shadow: 0 8px 16px rgba(15, 23, 42, 0.22);
    }
    .btn-secondary {
      background: #ffffff;
      color: #111827;
      border: 1px solid #e5e7eb;
      box-shadow: none;
    }
    .btn-secondary:hover {
      background: #f3f4f6;
      box-shadow: none;
      transform: none;
    }
    .btn-ghost {
      background: transparent;
      color: #4b5563;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      border: 1px solid transparent;
      box-shadow: none;
    }
    .btn-ghost:hover {
      background: #f3f4f6;
      border-color: #e5e7eb;
    }
    .badge {
      border-radius: 999px;
      font-size: 11px;
      padding: 2px 8px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 1px solid #e5e7eb;
      background: #f9fafb;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .badge-dot {
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: #22c55e;
    }
    .table-wrapper {
      border-radius: 14px;
      border: 1px solid #e5e7eb;
      overflow: hidden;
      background: #ffffff;
    }
    table {
      border-collapse: collapse;
      width: 100%;
      font-size: 13px;
    }
    thead {
      background: #f9fafb;
    }
    th, td {
      padding: 9px 12px;
      border-bottom: 1px solid #e5e7eb;
      text-align: left;
      white-space: nowrap;
    }
    th {
      font-weight: 500;
      color: #4b5563;
      font-size: 12px;
    }
    tr:last-child td {
      border-bottom: none;
    }
    .status-chip {
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .status-unused {
      background: #ecfdf5;
      color: #166534;
    }
    .status-used {
      background: #fef2f2;
      color: #b91c1c;
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: currentColor;
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
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
      gap: 20px;
    }
    @media (max-width: 900px) {
      .layout {
        grid-template-columns: minmax(0, 1fr);
      }
    }
    .filters {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .filters select {
      width: auto;
      min-width: 96px;
    }
    .filters .btn-ghost {
      margin-left: auto;
    }
    .pill {
      border-radius: 999px;
      border: 1px solid #e5e7eb;
      padding: 4px 8px;
      font-size: 11px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: #f9fafb;
      color: #4b5563;
    }
    .pill-label {
      text-transform: uppercase;
      letter-spacing: 0.09em;
      font-size: 10px;
      color: #9ca3af;
    }
    .logo {
      font-weight: 700;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      font-size: 13px;
    }
    .logo-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: linear-gradient(135deg, #6366f1, #ec4899);
      display: inline-block;
      margin-right: 4px;
    }
  </style>
</head>
<body>
  <div class="page">
    <header style="display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; gap:12px;">
      <div>
        <div class="logo"><span class="logo-dot"></span>WASSYL</div>
        <div class="muted" style="font-size:12px; margin-top:4px;">
          Panel administracyjny kart podarunkowych
        </div>
      </div>
      <span class="badge">
        <span class="badge-dot"></span>
        LIVE
      </span>
    </header>

    <main class="layout">
      <section class="card">
        <div class="card-header">
          <div>
            <div class="card-title">
              Dodaj nowe kody
              <span class="card-title-badge">input</span>
            </div>
            <p class="card-description">
              Wklej listę kodów, wybierz nominał i zapisz je do bazy. Każdy kod w osobnej linii.
            </p>
          </div>
        </div>

        <div style="margin-top:16px;">
          <div class="section-label">Lista kodów</div>
          <textarea id="codes-input" placeholder="Wpisz lub wklej kody, każdy w osobnej linii..."></textarea>
          <p class="muted" style="margin-top:4px;">
            Przykład:
            <code style="font-size:12px; background:#f3f4f6; padding:2px 4px; border-radius:6px;">
              ABC-123-XYZ
            </code>
          </p>
        </div>

        <div style="margin-top:16px;">
          <div class="section-label">Nominał</div>
          <select id="value-select">
            <option value="100">100 zł</option>
            <option value="200">200 zł</option>
            <option value="300">300 zł</option>
            <option value="500">500 zł</option>
          </select>
          <p class="muted" style="margin-top:4px;">
            Te kody zostaną dodane jako <strong>nieużyte</strong>.
          </p>
        </div>

        <div style="margin-top:18px; display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <div class="muted" id="codes-summary">
            Liczba kodów: <strong>0</strong>
          </div>
          <button class="btn" id="btn-add-codes">
            <span>➕</span>
            <span>Zapisz kody</span>
          </button>
        </div>
      </section>

      <section class="card">
        <div class="card-header">
          <div>
            <div class="card-title">
              Statystyki i ostatnie kody
              <span class="card-title-badge">monitoring</span>
            </div>
            <p class="card-description">
              Podgląd liczby kodów w bazie – użyte, nieużyte i łączna liczba dla każdego nominału.
            </p>
          </div>
          <button class="btn-secondary btn" id="btn-refresh-table">
            Odśwież
          </button>
        </div>

        <div class="chips" id="chips-summary">
          <!-- wypełniane z JS -->
        </div>

        <div style="margin-top:18px;">
          <div class="section-label">Filtry</div>
          <div class="filters">
            <select id="filter-value">
              <option value="">Wszystkie nominały</option>
              <option value="100">100 zł</option>
              <option value="200">200 zł</option>
              <option value="300">300 zł</option>
              <option value="500">500 zł</option>
            </select>
            <select id="filter-used">
              <option value="">Wszystkie statusy</option>
              <option value="unused">Nieużyte</option>
              <option value="used">Użyte</option>
            </select>
            <button class="btn-ghost" id="btn-clear-filters">
              Wyczyść filtry
            </button>
          </div>
        </div>

        <div style="margin-top:16px;">
          <div class="section-label">Ostatnie kody</div>
          <div class="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Kod</th>
                  <th>Nominał</th>
                  <th>Status</th>
                  <th>Numer zamówienia</th>
                </tr>
              </thead>
              <tbody id="codes-table-body">
                <tr>
                  <td colspan="4" class="muted" style="text-align:center; padding:20px;">
                    Ładowanie danych...
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="muted" style="margin-top:8px; font-size:12px;">
            Wyświetlane są najnowsze kody, maksymalnie 100 rekordów.
          </p>
        </div>
      </section>
    </main>
  </div>

  <script>
    const textarea = document.getElementById("codes-input");
    const summary = document.getElementById("codes-summary");

    function updateSummary() {
      const text = textarea.value.trim();
      if (!text) {
        summary.innerHTML = 'Liczba kodów: <strong>0</strong>';
        return;
      }
      const lines = text
        .split(/\\r?\\n/)
        .map((l) => l.trim())
        .filter((l) => l.length > 0);
      summary.innerHTML = 'Liczba kodów: <strong>' + lines.length + '</strong>';
    }

    textarea.addEventListener("input", updateSummary);
    updateSummary();

    async function addCodes() {
      const text = textarea.value.trim();
      if (!text) {
        alert("Wpisz przynajmniej jeden kod.");
        return;
      }

      const lines = text
        .split(/\\r?\\n/)
        .map((l) => l.trim())
        .filter((l) => l.length > 0);

      const valueSelect = document.getElementById("value-select");
      const value = parseInt(valueSelect.value, 10);

      const payload = {
        value: value,
        codes: lines,
      };

      try {
        const res = await fetch("/admin/api/codes", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          alert("Błąd przy zapisie kodów: " + (err.detail || res.statusText));
          return;
        }

        const data = await res.json();
        alert("Zapisano " + data.inserted + " kodów.");
        textarea.value = "";
        updateSummary();
        fetchStats();
        fetchCodes();
      } catch (e) {
        console.error(e);
        alert("Wystąpił błąd sieci przy zapisie kodów.");
      }
    }

    document.getElementById("btn-add-codes").addEventListener("click", addCodes);

    async function fetchStats() {
      const statsEl = document.getElementById("chips-summary");
      statsEl.innerHTML = "";

      try {
        const res = await fetch("/admin/api/stats");
        if (!res.ok) {
          statsEl.innerHTML =
            '<span class="muted">Błąd przy pobieraniu statystyk.</span>';
          return;
        }
        const data = await res.json();
        if (!data || data.length === 0) {
          statsEl.innerHTML =
            '<span class="muted">Brak danych statystycznych.</span>';
          return;
        }

        const labels = {
          100: "100 zł",
          200: "200 zł",
          300: "300 zł",
          500: "500 zł",
        };

        data.forEach((row) => {
          const div = document.createElement("div");
          div.className = "chip";
          const label = labels[row.value] || row.value + " zł";
          div.innerHTML =
            "<strong>" +
            label +
            "</strong>&nbsp;&nbsp;Łącznie: " +
            row.total +
            " &nbsp;•&nbsp; Nieużyte: " +
            row.unused +
            " &nbsp;•&nbsp; Użyte: " +
            row.used;
          statsEl.appendChild(div);
        });
      } catch (e) {
        console.error(e);
        statsEl.innerHTML =
          '<span class="muted">Błąd sieci przy pobieraniu statystyk.</span>';
      }
    }

    async function fetchCodes() {
      const tbody = document.getElementById("codes-table-body");
      tbody.innerHTML =
        '<tr><td colspan="4" class="muted" style="text-align:center; padding:20px;">Ładowanie danych...</td></tr>';

      const filterValue = document.getElementById("filter-value").value;
      const filterUsed = document.getElementById("filter-used").value;

      const params = new URLSearchParams();
      if (filterValue) params.set("value", filterValue);
      if (filterUsed) params.set("used", filterUsed);

      try {
        const res = await fetch("/admin/api/codes?" + params.toString());
        if (!res.ok) {
          tbody.innerHTML =
            '<tr><td colspan="4" class="muted" style="text-align:center; padding:20px;">Błąd przy pobieraniu kodów.</td></tr>';
          return;
        }
        const data = await res.json();
        if (!data || data.length === 0) {
          tbody.innerHTML =
            '<tr><td colspan="4" class="muted" style="text-align:center; padding:20px;">Brak kodów do wyświetlenia.</td></tr>';
          return;
        }

        tbody.innerHTML = "";
        data.forEach((row) => {
          const tr = document.createElement("tr");

          const tdCode = document.createElement("td");
          tdCode.textContent = row.code;
          tr.appendChild(tdCode);

          const tdValue = document.createElement("td");
          tdValue.textContent = row.value + " zł";
          tr.appendChild(tdValue);

          const tdStatus = document.createElement("td");
          const chip = document.createElement("span");
          chip.className = "status-chip " + (row.used ? "status-used" : "status-unused");
          const dot = document.createElement("span");
          dot.className = "status-dot";
          chip.appendChild(dot);
          const label = document.createElement("span");
          label.textContent = row.used ? "Użyty" : "Nieużyty";
          chip.appendChild(label);
          tdStatus.appendChild(chip);
          tr.appendChild(tdStatus);

          const tdOrder = document.createElement("td");
          tdOrder.textContent = row.order_id || "—";
          tr.appendChild(tdOrder);

          tbody.appendChild(tr);
        });
      } catch (e) {
        console.error(e);
        tbody.innerHTML =
          '<tr><td colspan="4" class="muted" style="text-align:center; padding:20px;">Błąd sieci przy pobieraniu kodów.</td></tr>';
      }
    }

    document.getElementById("btn-refresh-table").addEventListener("click", function () {
      fetchStats();
      fetchCodes();
    });

    document.getElementById("btn-clear-filters").addEventListener("click", function () {
      document.getElementById("filter-value").value = "";
      document.getElementById("filter-used").value = "";
      fetchCodes();
    });

    // pierwsze ładowanie
    fetchStats();
    fetchCodes();
  </script>
</body>
</html>
"""


# ------------------------------------------------------------------------------
# ROUTES: ROOT, HEALTH, DEBUG, ADMIN
# ------------------------------------------------------------------------------


@app.get("/", response_class=PlainTextResponse)
def root() -> str:
    return "WASSYL Giftcard Webhook – OK"


@app.get("/health")
def health_check():
    """
    Prosty endpoint sprawdzający kondycję aplikacji:
    - połączenie z bazą
    - dostępność klucza SendGrid
    - obecność pliku szablonu PDF
    - konfigurację IdosellClient
    """
    # 1. Baza danych
    db_status = "unknown"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    # 2. SendGrid
    from email_utils import SENDGRID_API_KEY as SG_KEY  # import lokalny, żeby nie robić cykli
    sendgrid_status = "configured" if SG_KEY else "missing"

    # 3. PDF template
    from pdf_utils import TEMPLATE_PATH  # ścieżka do WASSYL-GIFTCARD.pdf
    pdf_template_status = "found" if os.path.exists(TEMPLATE_PATH) else "missing"

    # 4. Idosell
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


@app.get("/debug/test-pdf")
def debug_test_pdf():
    """
    Generuje testowy PDF karty podarunkowej (bez wysyłki maila).
    """
    # przykładowe dane
    pdf_bytes = generate_giftcard_pdf(code="TEST-1234-ABCD", value=200)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="test-giftcard.pdf"'},
    )


@app.get("/debug/test-email")
def debug_test_email(to: str = Query(..., description="Adres e-mail odbiorcy testu")):
    """
    Wysyła testowy e-mail z przykładową kartą podarunkową w załączniku.
    """
    # generujemy prosty testowy PDF
    pdf_bytes = generate_giftcard_pdf(code="TEST-DEBUG-0001", value=100)

    send_email(
        to_email=to,
        subject="Test – WASSYL karta podarunkowa",
        body_text="To jest testowa wiadomość z załączoną kartą podarunkową (PDF).",
        body_html="<p>To jest <strong>testowa</strong> wiadomość z załączoną kartą podarunkową (PDF).</p>",
        attachments=[("test-giftcard.pdf", pdf_bytes)],
    )
    return {"status": "sent", "to": to}


@app.get("/debug/tables")
def debug_tables():
    """
    Zwraca listę tabel w schemacie public (do diagnostyki).
    """
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT tablename
                FROM pg_catalog.pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
        )
        tables = [row[0] for row in result]

    return {"tables": tables}


@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    """
    Prosty panel administracyjny (HTML + JS) do zarządzania kodami.
    """
    return HTMLResponse(content=ADMIN_HTML)


# ------------------------------------------------------------------------------
# ADMIN API – operacje na kodach
# ------------------------------------------------------------------------------


@app.get("/admin/api/stats")
def admin_stats():
    """
    Zwraca statystyki kodów według nominału:

    [
      {
        "value": 100,
        "total": 10,
        "used": 3,
        "unused": 7
      },
      ...
    ]

    Użycie kodu liczymy po tym, czy order_id jest ustawione (NOT NULL).
    """
    db = SessionLocal()
    try:
        result = db.execute(
            text(
                """
                SELECT
                  value,
                  COUNT(*) AS total,
                  COUNT(order_id) AS used,
                  COUNT(*) - COUNT(order_id) AS unused
                FROM gift_codes
                GROUP BY value
                ORDER BY value
                """
            )
        )
        rows = result.fetchall()
        stats = [
            {
                "value": row.value,
                "total": row.total,
                "used": row.used,
                "unused": row.unused,
            }
            for row in rows
        ]
        return stats
    except SQLAlchemyError as e:
        logger.exception("Błąd podczas pobierania statystyk: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()


@app.get("/admin/api/codes")
def admin_list_codes(
    value: Optional[int] = Query(None, description="Filtr po nominale (np. 100, 200)"),
    used: Optional[str] = Query(
        None, description="Filtr statusu: 'used' lub 'unused'"
    ),
    limit: int = Query(100, ge=1, le=500, description="Maksymalna liczba rekordów"),
):
    """
    Zwraca listę ostatnich kodów z możliwością filtrowania.

    Pole 'used' wyliczamy na podstawie tego, czy order_id jest ustawione.
    """
    db = SessionLocal()
    try:
        conditions = []
        params: Dict[str, Any] = {}

        if value is not None:
            conditions.append("value = :value")
            params["value"] = value

        if used is not None:
            if used == "used":
                conditions.append("order_id IS NOT NULL")
            elif used == "unused":
                conditions.append("order_id IS NULL")

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = text(
            f"""
            SELECT id, code, value, order_id
            FROM gift_codes
            {where_clause}
            ORDER BY id DESC
            LIMIT :limit
            """
        )
        params["limit"] = limit

        result = db.execute(query, params)
        rows = result.fetchall()
        codes = [
            {
                "id": row.id,
                "code": row.code,
                "value": row.value,
                # 'used' liczymy po order_id
                "used": row.order_id is not None,
                "order_id": row.order_id,
            }
            for row in rows
        ]
        return codes
    except SQLAlchemyError as e:
        logger.exception("Błąd podczas pobierania listy kodów: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()


@app.post("/admin/api/codes")
def admin_add_codes(payload: Dict[str, Any]):
    """
    Dodaje nowe kody do puli dla danego nominału.

    payload może wyglądać tak:
      { "value": 100, "codes": "KOD1\nKOD2\nKOD3" }  # string
      lub
      { "value": 100, "codes": ["KOD1", "KOD2", "KOD3"] }  # lista
    """
    value = int(payload.get("value"))

    codes_raw = payload.get("codes") or ""

    # Obsługa obu formatów: string i lista
    if isinstance(codes_raw, str):
        codes = [c.strip() for c in codes_raw.splitlines() if c.strip()]
    elif isinstance(codes_raw, list):
        codes = [str(c).strip() for c in codes_raw if str(c).strip()]
    else:
        codes = []

    if not codes:
        raise HTTPException(status_code=400, detail="Brak kodów do dodania")

    db = SessionLocal()
    try:
        for code in codes:
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
            "Dodano %s nowych kodów dla nominału %s",
            len(codes),
            value,
        )
        return {"status": "ok", "added": len(codes)}

    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Błąd podczas dodawania nowych kodów: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()
