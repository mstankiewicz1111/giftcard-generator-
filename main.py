import logging
import os
import json
import io
import zipfile
import csv
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
from pdf_utils import generate_giftcard_pdf, TEMPLATE_PATH
from email_utils import send_giftcard_email, send_email, SENDGRID_API_KEY, SENDGRID_FROM_EMAIL
from idosell_client import IdosellClient, IdosellApiError

# ------------------------------------------------------------------------------
# Konfiguracja aplikacji i logowania
# ------------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("giftcard-webhook")

app = FastAPI(title="WASSYL Giftcard Webhook")

# Inicjalizacja bazy (w tym nowej tabeli webhook_events)
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
        "Brak konfiguracji IDOSELL_DOMAIN/IDOSELL_API_KEY – integracja z Idosell będzie nieaktywna."
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

    Fix: Idosell często trzyma nominał nie w productName, tylko w sizePanelName.
    """
    result: List[Dict[str, Any]] = []

    order_details = order.get("orderDetails") or {}

    # Idosell w Twoim payloadzie używa 'productsResults'
    products = order_details.get("productsResults") or []
    # gdyby kiedyś pojawiło się 'basket', też je obsłużymy:
    if not products:
        products = order_details.get("basket") or []

    for item in products:
        try:
            product_id = int(item.get("productId") or 0)
        except (TypeError, ValueError):
            continue

        if product_id != GIFT_PRODUCT_ID:
            continue

        # Idosell: nominał może być w różnych polach (np. sizePanelName = "200 zł")
        variant_text_parts = [
            item.get("productName"),
            item.get("sizePanelName"),
            item.get("sizeName"),
            item.get("versionName"),
        ]
        variant_text = " ".join(str(p) for p in variant_text_parts if p).strip()

        matched_value: Optional[int] = None
        for label, val in GIFT_VARIANTS.items():
            if label in variant_text:
                matched_value = val
                break

        # dodatkowy fallback: jeśli nie ma etykiety "200 zł", spróbuj wyciągnąć liczbę
        # z sizePanelName / sizeName (np. "200 zł", "200zl", "200")
        if matched_value is None:
            raw = (item.get("sizePanelName") or item.get("sizeName") or "").strip()
            digits = "".join(ch for ch in str(raw) if ch.isdigit())
            if digits:
                try:
                    maybe = int(digits)
                    if maybe in set(GIFT_VARIANTS.values()):
                        matched_value = maybe
                except ValueError:
                    pass

        if matched_value is None:
            continue

        quantity = int(item.get("productQuantity") or item.get("quantity") or 1)
        result.append({"value": matched_value, "quantity": quantity})

    return result


def _is_order_paid(order: Dict[str, Any]) -> bool:
    """
    Sprawdza, czy zamówienie jest opłacone.
    Zakładamy, że w orderDetails.prepaids[*].paymentStatus == 'y' oznacza opłacone.
    """
    order_details = order.get("orderDetails") or {}
    prepaids = order_details.get("prepaids") or []
    return any(p.get("paymentStatus") == "y" for p in prepaids)


def log_webhook_event(
    status: str,
    message: str,
    payload: Any,
    order_id: Optional[str] = None,
    order_serial: Optional[str] = None,
    event_type: str = "order_webhook",
) -> None:
    """
    Zapisuje prosty log webhooka w tabeli webhook_events.
    Błędy logowania nie blokują obsługi webhooka.
    """
    try:
        db = SessionLocal()
        db.execute(
            text(
                """
                INSERT INTO webhook_events (
                    event_type, status, message,
                    order_id, order_serial, payload
                )
                VALUES (:event_type, :status, :message, :order_id, :order_serial, :payload)
                """
            ),
            {
                "event_type": event_type,
                "status": status,
                "message": (message or "")[:500],
                "order_id": order_id,
                "order_serial": str(order_serial) if order_serial is not None else None,
                "payload": json.dumps(payload, ensure_ascii=False)[:8000],
            },
        )
        db.commit()
    except Exception as e:
        logger.exception("Nie udało się zapisać logu webhooka: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


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
        msg = "Webhook /webhook/order: brak lub nieprawidłowa sekcja 'order'."
        logger.error("%s Payload: %s", msg, payload)
        log_webhook_event(
            status="bad_request",
            message=msg,
            payload=payload,
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

    # 1. Sprawdzamy, czy zamówienie jest opłacone
    if not _is_order_paid(order):
        msg = "Zamówienie nie jest opłacone – ignoruję webhook."
        logger.info(
            "Zamówienie %s (serial: %s) nie jest opłacone – ignoruję.",
            order_id,
            order_serial,
        )
        log_webhook_event(
            status="ignored_unpaid",
            message=msg,
            payload=order,
            order_id=order_id,
            order_serial=str(order_serial) if order_serial is not None else None,
        )
        return JSONResponse(
            {"status": "ignored", "reason": "unpaid"},
            status_code=200,
        )

    # 2. Wyciągamy pozycje kart podarunkowych
    gift_positions = _extract_giftcard_positions(order)
    if not gift_positions:
        msg = "Opłacone zamówienie nie zawiera kart podarunkowych – ignoruję."
        logger.info(
            "Opłacone zamówienie %s nie zawiera kart podarunkowych – ignoruję.",
            order_id,
        )
        log_webhook_event(
            status="ignored_no_giftcards",
            message=msg,
            payload=order,
            order_id=order_id,
            order_serial=str(order_serial) if order_serial is not None else None,
        )
        return JSONResponse(
            {"status": "ok", "reason": "no_giftcards"},
            status_code=200,
        )

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
                    db.rollback()
                    log_webhook_event(
                        status="error",
                        message=f"Brak kodów dla nominału {value}",
                        payload=order,
                        order_id=order_id,
                        order_serial=order_serial_str,
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
        logger.exception(
            "Błąd podczas przydzielania kodów dla zamówienia %s (%s): %s",
            order_id,
            order_serial,
            e,
        )
        log_webhook_event(
            status="error",
            message=f"Błąd przydzielania kodów: {e}",
            payload=order,
            order_id=order_id,
            order_serial=str(order_serial) if order_serial is not None else None,
        )
        raise
    finally:
        db.close()

    # 4. Wysyłka e-maila z kartą/kartami – TYLKO przy pierwszym przydzieleniu
    #    (jeśli assigned_codes jest puste, to prawdopodobnie retry webhooka)
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
            "Brak e-maila klienta lub brak NOWO przypisanych kodów dla zamówienia %s – pomijam wysyłkę maila (prawdopodobnie retry).",
            order_id,
        )

    # 5. Aktualizacja notatki zamówienia w Idosell (tylko gdy są nowe kody)
    if assigned_codes and order_serial and idosell_client:
        order_serial_str = str(order_serial).strip()

        codes_text = ", ".join(f"{c['code']} ({c['value']} zł)" for c in assigned_codes)
        note_text = f"Numer(y) karty podarunkowej: {codes_text}"

        logger.info(
            "Idosell: próba aktualizacji notatki zamówienia serial=%s; note='%s'",
            order_serial_str,
            note_text,
        )

        try:
            idosell_client.update_order_note(order_serial_str, note_text)
            log_webhook_event(
                status="idosell_note_updated",
                message=f"Zaktualizowano notatkę: {note_text}",
                payload={"note": note_text},
                order_id=order_id,
                order_serial=order_serial_str,
                event_type="idosell_note",
            )
        except IdosellApiError as e:
            logger.error(
                "Błąd IdosellApiError przy aktualizacji notatki zamówienia %s: %s",
                order_serial_str,
                e,
            )
            log_webhook_event(
                status="idosell_note_error",
                message=f"IdosellApiError: {e}",
                payload={"note": note_text},
                order_id=order_id,
                order_serial=order_serial_str,
                event_type="idosell_note",
            )
        except Exception as e:
            logger.exception(
                "Nieoczekiwany błąd przy aktualizacji notatki zamówienia %s: %s",
                order_serial_str,
                e,
            )
            log_webhook_event(
                status="idosell_note_error",
                message=f"Unexpected: {e}",
                payload={"note": note_text},
                order_id=order_id,
                order_serial=order_serial_str,
                event_type="idosell_note",
            )
    elif assigned_codes and not idosell_client:
        logger.warning(
            "Brak skonfigurowanego klienta Idosell – pomijam aktualizację notatki dla zamówienia %s.",
            order_id,
        )

    # Log sukcesu webhooka

    log_webhook_event(
        status="processed",
        message=f"Przydzielono {len(assigned_codes)} nowych kodów.",
        payload=order,
        order_id=order_id,
        order_serial=str(order_serial) if order_serial is not None else None,
    )

    return {
        "status": "processed",
        "orderId": order_id,
        "orderSerialNumber": order_serial,
        "assigned_codes": assigned_codes,
    }


# ------------------------------------------------------------------------------
# PROSTE ENDPOINTY POMOCNICZE / DEBUG
# ------------------------------------------------------------------------------


@app.get("/", response_class=PlainTextResponse)
def root():
    return PlainTextResponse("WASSYL Giftcard Webhook – działa.")


@app.get("/health")
def health_check():
    """
    Sprawdzenie:
    - połączenia z DB
    - konfiguracji SendGrid
    - obecności szablonu PDF
    - konfiguracji Idosell
    """
    db_ok = False
    sendgrid_ok = False
    pdf_ok = False
    idosell_ok = False

    # DB
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.exception("Healthcheck DB failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass

    # SendGrid – tylko sprawdzamy czy jest skonfigurowany klucz i nadawca
    sendgrid_ok = bool(SENDGRID_API_KEY and SENDGRID_FROM_EMAIL)

    # PDF template
    pdf_ok = bool(TEMPLATE_PATH and os.path.exists(TEMPLATE_PATH))

    # Idosell
    idosell_ok = idosell_client is not None

    status_code = 200 if db_ok and sendgrid_ok and pdf_ok else 503

    return JSONResponse(
        {
            "database": db_ok,
            "sendgrid_configured": sendgrid_ok,
            "pdf_template_found": pdf_ok,
            "idosell_configured": idosell_ok,
        },
        status_code=status_code,
    )


@app.get("/debug/test-pdf")
def debug_test_pdf():
    """
    Generuje testowy PDF karty podarunkowej (bez wysyłki maila).
    """
    pdf_bytes = generate_giftcard_pdf(code="TEST-1234-ABCD", value=200)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="test-giftcard.pdf"'},
    )


@app.get("/debug/test-email")
def debug_test_email(to: str = Query(..., description="Adres e-mail odbiorcy testu")):
    """
    Wysyła testowy e-mail z docelowym HTML-em i przykładową kartą podarunkową w załączniku.
    """
    pdf_bytes = generate_giftcard_pdf(code="TEST-DEBUG-0001", value=100)

    send_email(
        to_email=to,
        subject="Test – WASSYL karta podarunkowa",
        body_text=(
            "To jest testowa wiadomość z załączoną kartą podarunkową (PDF).\n"
            "Treść HTML odpowiada docelowemu mailowi produkcyjnemu."
        ),
        body_html=None,  # send_email samo zbuduje HTML jeśli None, ale tu nie nadpisujemy szablonu produkcyjnego
        attachments=[("test-giftcard.pdf", pdf_bytes)],
    )

    return PlainTextResponse(f"Wysłano testowy e-mail na adres: {to}")


@app.get("/debug/tables")
def debug_tables():
    """
    Zwraca listę tabel w schemacie public.
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT tablename
                FROM pg_catalog.pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
        ).fetchall()
        tables = [r[0] for r in rows]
        return {"tables": tables}
    finally:
        db.close()


# ------------------------------------------------------------------------------
# PROSTY PANEL ADMINA (HTML + JS)
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
      background: #0f172a;
      color: #111827;
    }
    * {
      box-sizing: border-box;
    }
    .app {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: stretch;
      padding: 24px 12px;
    }
    @media (min-width: 768px) {
      .app {
        padding: 32px;
      }
    }
    header {
      max-width: 1100px;
      margin: 0 auto 16px auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .logo {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .logo img {
      height: 32px;
      width: auto;
    }
    .logo-title {
      font-size: 18px;
      font-weight: 600;
      letter-spacing: -0.02em;
      color: #e5e7eb;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 11px;
      color: #f9fafb;
      background: rgba(34, 197, 94, 0.2);
      border: 1px solid rgba(34, 197, 94, 0.5);
    }
    .badge-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #22c55e;
      box-shadow: 0 0 0 6px rgba(34, 197, 94, 0.25);
    }
    main.layout {
      max-width: 1100px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
      gap: 16px;
      align-items: flex-start;
    }
    @media (max-width: 960px) {
      main.layout {
        grid-template-columns: minmax(0, 1fr);
      }
    }
    .card {
      background: #ffffff;
      border-radius: 16px;
      padding: 18px 18px 16px 18px;
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.50);
      border: 1px solid rgba(148, 163, 184, 0.35);
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 8px;
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
      padding: 3px 10px;
      background: #eef2ff;
      color: #3730a3;
      border: 1px solid rgba(129, 140, 248, 0.6);
    }
    .card-description {
      font-size: 13px;
      color: #6b7280;
      margin: 4px 0 0 0;
    }
    .section-label {
      font-size: 13px;
      font-weight: 600;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      color: #6b7280;
      margin-bottom: 6px;
    }
    textarea {
      width: 100%;
      min-height: 120px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid #e5e7eb;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono";
      font-size: 13px;
      resize: vertical;
    }
    
    input {
      width: 100%;
      padding: 10px 12px;
      background: #0b1225;
      border: 1px solid rgba(148, 163, 184, 0.25);
      border-radius: 10px;
      color: #e5e7eb;
      outline: none;
      font-size: 14px;
    }
    input:focus {
      border-color: rgba(56, 189, 248, 0.7);
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
    }
textarea:focus {
      outline: none;
      border-color: #4f46e5;
      box-shadow: 0 0 0 1px rgba(79, 70, 229, 0.45);
    }
    .muted {
      font-size: 12px;
      color: #6b7280;
    }
    select, input[type="number"] {
      padding: 7px 9px;
      border-radius: 10px;
      border: 1px solid #e5e7eb;
      font-size: 13px;
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
      padding: 8px 10px;
      border-bottom: 1px solid #e5e7eb;
      text-align: left;
      white-space: nowrap;
    }
    tbody tr:hover {
      background: #f9fafb;
    }
    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 500;
    }
    .status-used {
      background: rgba(220, 38, 38, 0.06);
      color: #991b1b;
    }
    .status-unused {
      background: rgba(5, 150, 105, 0.06);
      color: #166534;
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 999px;
    }
    .status-used .status-dot {
      background: #dc2626;
    }
    .status-unused .status-dot {
      background: #22c55e;
    }
    .btn-row {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 12px;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      border: none;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      background: linear-gradient(135deg, #0f172a, #020617);
      color: #f9fafb;
      box-shadow: 0 16px 35px rgba(15, 23, 42, 0.6);
      transition: transform 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.7);
      background: #020617;
    }
    .btn:active {
      transform: translateY(0);
      box-shadow: 0 10px 18px rgba(15, 23, 42, 0.5);
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
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .chip {
      font-size: 12px;
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid #e5e7eb;
      background: #f9fafb;
      color: #374151;
    }
    .filter-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin: 8px 0 10px 0;
    }
    .filter-row label {
      font-size: 12px;
      color: #6b7280;
    }
    .logs-table td:nth-child(3),
    .logs-table td:nth-child(4) {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono";
      font-size: 12px;
    }
  
.btn-primary{
  background: linear-gradient(135deg, #4f46e5, #2563eb);
  color:#fff;
  border:none;
  box-shadow: 0 16px 35px rgba(37, 99, 235, 0.35);
}
.btn-primary:hover{ background:#1d4ed8; }

.pill{
  display:inline-flex;
  align-items:center;
  gap:6px;
  padding:3px 10px;
  border-radius:999px;
  font-size:12px;
  font-weight:600;
  border:1px solid #e5e7eb;
  background:#f9fafb;
  color:#111827;
}
.pill.ok{ background: rgba(34,197,94,0.12); border-color: rgba(34,197,94,0.35); color:#166534; }
.pill.err{ background: rgba(220,38,38,0.10); border-color: rgba(220,38,38,0.35); color:#991b1b; }
.pill.warn{ background: rgba(245,158,11,0.12); border-color: rgba(245,158,11,0.35); color:#92400e; }

</style>
</head>
<body>
  <div class="app">
    <header>
      <div class="logo">
        <img src="https://wassyl.pl/data/include/cms/gfx/logo-wassyl.png" alt="WASSYL" />
        <div class="logo-title">
          Panel administracyjny kart podarunkowych
        </div>
      </div>
      <span class="badge">
        <span class="badge-dot"></span>
        LIVE
      </span>
    </header>

    <main class="layout">
      <!-- Lewa kolumna: dodawanie kodów -->
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

        <div>
          <div class="section-label">Lista kodów</div>
          <textarea id="codes-input" placeholder="Wpisz lub wklej kody, każdy w osobnej linii..."></textarea>
          <p class="muted" id="codes-summary" style="margin-top:4px;">
            Liczba kodów: <strong>0</strong>
          </p>
        </div>

        <div style="margin-top:12px;">
          <div class="section-label">Nominał</div>
          <select id="nominal-select">
            <option value="100">100 zł</option>
            <option value="200">200 zł</option>
            <option value="300">300 zł</option>
            <option value="500">500 zł</option>
          </select>
        </div>

        <div class="btn-row">
          <button class="btn" id="btn-save-codes">
            <span>➕</span>
            <span>Zapisz kody</span>
          </button>
        </div>
      </section>

      

<!-- Korekta nominału -->
<section class="card" style="margin-top: 16px;">
  <div class="card-header">
    <div>
      <div class="card-title">
        Korekta nominału kodów
        <span class="card-title-badge">tools</span>
      </div>
      <p class="card-description">
        Wklej kody (1 wiersz = 1 kod) i ustaw docelowy nominał. System zmieni tylko kody <strong>nieprzypisane</strong> (order_id = NULL).
      </p>
    </div>
  </div>

  <div class="form-row">
    <div class="field">
      <label class="label" for="correct-new-value">Docelowy nominał</label>
      <select id="correct-new-value" class="input">
        <option value="100">100 zł</option>
        <option value="200" selected>200 zł</option>
        <option value="300">300 zł</option>
        <option value="500">500 zł</option>
      </select>
    </div>
  </div>

  <div class="field" style="margin-top: 10px;">
    <label class="label" for="correct-codes-input">Lista kodów do korekty</label>
    <textarea id="correct-codes-input" class="textarea" placeholder="KOD1&#10;KOD2&#10;KOD3"></textarea>
    <div class="muted" style="margin-top:6px; font-size:12px;">
      Tip: duplikaty wklejone w polu zostaną automatycznie pominięte.
    </div>
  </div>

  <div class="btn-row" style="margin-top: 12px;">
    <button class="btn" id="btn-correct-value">
      <span>🛠️</span>
      <span>Zmień nominał</span>
    </button>
    <div id="correct-result" class="muted" style="margin-left: 10px;"></div>
  </div>
</section>

      <!-- Ręczne wygenerowanie karty -->
      <section class="card" style="margin-top: 16px;">
        <div class="card-header">
          <div>
            <div class="card-title">
              Ręczne wygenerowanie karty
              <span class="card-title-badge">manual</span>
            </div>
            <p class="card-description">
              Przypisz (lub pobierz istniejący) numer karty do zamówienia. Opcjonalnie pobierz PDF i/lub wyślij e-mail od razu do klienta.
            </p>
          </div>
        </div>

        <div class="row" style="grid-template-columns: 1fr; gap: 12px;">
          <div class="row" style="grid-template-columns: 1fr 1fr; gap: 12px;">
            <div>
              <div class="section-label">Wartość karty (PLN)</div>
              <input id="manual-value" type="number" min="1" step="1" placeholder="np. 200" />
            </div>
            <div>
              <div class="section-label">Numer zamówienia</div>
              <input id="manual-order" type="text" placeholder="orderSerialNumber (np. 1842586)" />
            </div>
          </div>

          <div>
            <div class="section-label">Adres e-mail do wysyłki</div>
            <input id="manual-email" type="email" placeholder="np. klient@domena.pl" />
            <div class="muted" style="margin-top: 6px;">
              Zabezpieczenie: jeśli ten numer zamówienia ma już przypisany kod, system zwróci istniejący (nie utworzy nowego).
            </div>
          </div>

          <div class="row" style="grid-template-columns: 1fr 1fr; gap: 12px; align-items: end;">
            <div>
              <label style="display:flex; gap:8px; align-items:center; user-select:none;">
                <input id="manual-attach-pdf" type="checkbox" />
                <span>Załącz PDF przy wysyłce e-mail</span>
              </label>
            </div>
            <div style="display:flex; gap:10px; justify-content:flex-end; flex-wrap:wrap;">
              <button class="btn" onclick="manualIssue()">Wygeneruj / pobierz</button>
              <button class="btn btn-secondary" id="manual-download-btn" onclick="manualDownloadPdf()" disabled>Pobierz PDF</button>
              <button class="btn btn-primary" id="manual-email-btn" onclick="manualSendEmail()" disabled>Wyślij wiadomość e-mail</button>
            </div>
          </div>

          <div id="manual-result" class="muted" style="margin-top: 6px;"></div>
        </div>
      </section>

<!-- Prawa kolumna: statystyki, lista kodów, eksport -->
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
        </div>

        <div>
          <div class="section-label">Statystyki</div>
          <div id="stats-container" class="chips">
            <span class="muted">Ładowanie statystyk...</span>
          </div>
        </div>

        <div style="margin-top:16px;">
          <div class="section-label">Ostatnie kody</div>
          <div class="filter-row">
            <label>
              Nominał:
              <select id="filter-value">
                <option value="">Wszystkie</option>
                <option value="100">100 zł</option>
                <option value="200">200 zł</option>
                <option value="300">300 zł</option>
                <option value="500">500 zł</option>
              </select>
            </label>
            <label>
              Status:
              <select id="filter-used">
                <option value="">Wszystkie</option>
                <option value="unused">Tylko nieużyte</option>
                <option value="used">Tylko użyte</option>
              </select>
            </label>
            <button class="btn-secondary" id="btn-refresh-codes">Odśwież</button>
            <button class="btn-secondary" id="btn-export-csv">Eksport CSV</button>
          </div>

          <div style="max-height: 260px; overflow:auto; border-radius: 10px; border: 1px solid #e5e7eb;">
            <table>
              <thead>
                <tr>
                  <th>Kod</th>
                  <th>Nominał</th>
                  <th>Status</th>
                  <th>Order ID</th>
                </tr>
              </thead>
              <tbody id="codes-tbody">
                <tr>
                  <td colspan="4" class="muted" style="text-align:center; padding:20px;">
                    Ładowanie danych...
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="muted" style="margin-top:8px; font-size:12px;">
            Wyświetlane są najnowsze kody, domyślnie maksymalnie 100 rekordów.
          </p>
        </div>
      </section>

      <!-- Druga karta: logi webhooka -->
      <section class="card">
        <div class="card-header">
          <div>
            <div class="card-title">
              Logi webhooka
              <span class="card-title-badge">debug</span>
            </div>
            <p class="card-description">
              Ostatnie 50 wywołań /webhook/order – status, numer zamówienia, krótka wiadomość.
            </p>
          </div>
          <button class="btn-secondary" id="btn-refresh-logs">Odśwież</button>
        </div>

        <div style="max-height: 260px; overflow:auto; border-radius: 10px; border: 1px solid #e5e7eb;">
          <table class="logs-table">
            <thead>
              <tr>
                <th>Data</th>
                <th>Status</th>
                <th>orderId</th>
                <th>Serial</th>
                <th>Komunikat</th>
              </tr>
            </thead>
            <tbody id="logs-tbody">
              <tr>
                <td colspan="5" class="muted" style="text-align:center; padding:20px;">
                  Ładowanie logów...
                </td>
              </tr>
            </tbody>
          </table>
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

    async function saveCodes() {
      const nominalSelect = document.getElementById("nominal-select");
      const value = parseInt(nominalSelect.value, 10);
      const text = textarea.value.trim();

      if (!text) {
        alert("Wpisz przynajmniej jeden kod.");
        return;
      }

      const lines = text
        .split(/\\r?\\n/)
        .map((l) => l.trim())
        .filter((l) => l.length > 0);

      if (lines.length === 0) {
        alert("Brak poprawnych linii z kodami.");
        return;
      }

      try {
        const res = await fetch("/admin/api/codes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            value: value,
            codes: lines
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          alert("Błąd podczas zapisywania kodów: " + (err.detail || res.status));
          return;
        }
        const data = await res.json();
        alert("Zapisano " + data.inserted + " kodów.");
        textarea.value = "";
        updateSummary();
        loadStats();
        loadCodes();
      } catch (e) {
        console.error(e);
        alert("Wystąpił błąd przy komunikacji z serwerem.");
      }
    }

    async function loadStats() {
      const statsEl = document.getElementById("stats-container");
      statsEl.innerHTML = '<span class="muted">Ładowanie statystyk...</span>';

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

        statsEl.innerHTML = "";
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
          '<span class="muted">Błąd przy pobieraniu statystyk.</span>';
      }
    }

    async function loadCodes() {
      const tbody = document.getElementById("codes-tbody");
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
          '<tr><td colspan="4" class="muted" style="text-align:center; padding:20px;">Błąd przy pobieraniu kodów.</td></tr>';
      }
    }

      async function correctValue() {
    const out = document.getElementById("correct-result");
    const codesText = (document.getElementById("correct-codes-input").value || "").trim();
    const newValue = parseInt(document.getElementById("correct-new-value").value || "0", 10);

    if (!codesText) {
      out.innerHTML = '<span class="pill err">Błąd</span> Wklej listę kodów.';
      return;
    }
    if (!newValue) {
      out.innerHTML = '<span class="pill err">Błąd</span> Wybierz docelowy nominał.';
      return;
    }

    out.innerHTML = '<span class="pill">Korekta</span> Przetwarzam…';

    try {
      const res = await fetch("/admin/api/codes/correct-value", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ newValue: newValue, codes: codesText })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Błąd korekty");

      out.innerHTML =
        '<span class="pill ok">OK</span> Zmieniono: <strong>' + data.updated +
        '</strong> • pominięto przypisane: <strong>' + data.skipped_assigned +
        '</strong> • nie znaleziono: <strong>' + data.not_found + '</strong>.';

      loadStats();
      loadCodes();
    } catch (e) {
      out.innerHTML = '<span class="pill err">Błąd</span> ' + (e.message || e);
    }
  }

  async function loadLogs() {
    const tbody = document.getElementById("logs-tbody");
    tbody.innerHTML =
      '<tr><td colspan="5" class="muted" style="text-align:center; padding:20px;">Ładowanie logów...</td></tr>';

    try {
      const res = await fetch("/admin/api/logs");
      if (!res.ok) {
        tbody.innerHTML =
          '<tr><td colspan="5" class="muted" style="text-align:center; padding:20px;">Błąd przy pobieraniu logów.</td></tr>';
        return;
      }

      const data = await res.json();
      if (!data || data.length === 0) {
        tbody.innerHTML =
          '<tr><td colspan="5" class="muted" style="text-align:center; padding:20px;">Brak logów do wyświetlenia.</td></tr>';
        return;
      }

      tbody.innerHTML = "";
      data.forEach((row) => {
        const tr = document.createElement("tr");

        const tdDate = document.createElement("td");
        tdDate.textContent = row.created_at || "—";
        tr.appendChild(tdDate);

        const tdStatus = document.createElement("td");
        tdStatus.textContent = row.status || "";
        tr.appendChild(tdStatus);

        const tdOrderId = document.createElement("td");
        tdOrderId.textContent = row.order_id || "—";
        tr.appendChild(tdOrderId);

        const tdSerial = document.createElement("td");
        tdSerial.textContent = row.order_serial || "—";
        tr.appendChild(tdSerial);

        const tdMsg = document.createElement("td");
        tdMsg.textContent = row.message || "";
        tr.appendChild(tdMsg);

        tbody.appendChild(tr);
      });
    } catch (e) {
      console.error(e);
      tbody.innerHTML =
        '<tr><td colspan="5" class="muted" style="text-align:center; padding:20px;">Błąd przy pobieraniu logów.</td></tr>';
    }
  }

    function exportCsv() {
      const filterValue = document.getElementById("filter-value").value;
      const filterUsed = document.getElementById("filter-used").value;

      const params = new URLSearchParams();
      if (filterValue) params.set("value", filterValue);
      if (filterUsed) params.set("used", filterUsed);

      const url = "/admin/api/codes/export" + (params.toString() ? "?" + params.toString() : "");
      window.open(url, "_blank");
    }

    document.getElementById("btn-save-codes").addEventListener("click", saveCodes);
    document.getElementById("btn-correct-value").addEventListener("click", correctValue);
    document.getElementById("btn-refresh-codes").addEventListener("click", loadCodes);
    document.getElementById("btn-export-csv").addEventListener("click", exportCsv);
    document.getElementById("btn-refresh-logs").addEventListener("click", loadLogs);

    // initial load
    loadStats();
    loadCodes();
    loadLogs();
  
    // -------------------------------
    // Manual issue / PDF / email
    // -------------------------------
    let manualLast = null;

    function setManualButtons(enabled) {
      document.getElementById("manual-download-btn").disabled = !enabled;
      document.getElementById("manual-email-btn").disabled = !enabled;
    }

    async function manualIssue() {
      const value = parseInt(document.getElementById("manual-value").value, 10);
      const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
      const email = (document.getElementById("manual-email").value || "").trim();
      const out = document.getElementById("manual-result");

      manualLast = null;
      setManualButtons(false);

      if (!value || value <= 0) {
        out.innerHTML = '<span class="pill warn">Brak danych</span> Podaj poprawną wartość karty.';
        return;
      }
      if (!orderSerialNumber) {
        out.innerHTML = '<span class="pill warn">Brak danych</span> Podaj numer zamówienia.';
        return;
      }
      if (!email) {
        out.innerHTML = '<span class="pill warn">Brak danych</span> Podaj adres e-mail.';
        return;
      }

      out.innerHTML = '<span class="pill">Przetwarzanie</span> Sprawdzam i przypisuję kod...';

      try {
        const res = await fetch("/admin/api/manual/issue", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ value, orderSerialNumber, email })
        });

        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Błąd API");
        }

        manualLast = data;
        setManualButtons(true);

        const reused = data.reused ? "TAK (z bazy)" : "NIE (nowy kod)";
        const note = data.noteUpdated ? "Notatka w Idosell: zaktualizowana." : "";
        out.innerHTML =
          '<span class="pill ok">OK</span> ' +
          'Kod: <strong>' + data.code + '</strong> (' + data.value + ' zł) • Zamówienie: <strong>' + data.orderSerialNumber + '</strong>' +
          ' • Reuse: <strong>' + reused + '</strong>. ' + note;

      } catch (e) {
        out.innerHTML = '<span class="pill err">Błąd</span> ' + (e.message || e);
      }
    }

    async function manualDownloadPdf() {
      const out = document.getElementById("manual-result");
      const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
      if (!orderSerialNumber) return;

      out.innerHTML = '<span class="pill">PDF</span> Generuję plik...';
      const url = "/admin/api/manual/pdf?orderSerialNumber=" + encodeURIComponent(orderSerialNumber);
      window.location.href = url;
      // nie wiemy czy przeglądarka pokaże download/preview – status zostawiamy:
      setTimeout(() => {
        if (manualLast) {
          out.innerHTML = '<span class="pill ok">OK</span> PDF wygenerowany dla zamówienia <strong>' + orderSerialNumber + '</strong>.';
        }
      }, 400);
    }

    async function manualSendEmail() {
      const out = document.getElementById("manual-result");
      const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
      const email = (document.getElementById("manual-email").value || "").trim();
      const attachPdf = document.getElementById("manual-attach-pdf").checked;

      if (!orderSerialNumber || !email) return;

      out.innerHTML = '<span class="pill">E-mail</span> Wysyłam wiadomość...';

      try {
        const res = await fetch("/admin/api/manual/send-email", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ orderSerialNumber, email, attachPdf })
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Błąd wysyłki");
        }

        out.innerHTML = '<span class="pill ok">Wysłano</span> Na: <strong>' + data.sentTo + '</strong> • PDF: <strong>' + (data.attachPdf ? "tak" : "nie") + '</strong>.';

      } catch (e) {
        out.innerHTML = '<span class="pill err">Błąd</span> ' + (e.message || e);
      }
    }
</script>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    """
    Prosty panel administracyjny (HTML + JS) do zarządzania kodami i podglądu logów webhooka.
    """
    return HTMLResponse(content=ADMIN_HTML)


# ------------------------------------------------------------------------------
# ADMIN API – operacje na kodach i logach
# ------------------------------------------------------------------------------


@app.get("/admin/api/stats")
def admin_stats():
    """
    Zwraca statystyki kodów (po nominale).
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT
                  value,
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE order_id IS NULL) AS unused,
                  COUNT(*) FILTER (WHERE order_id IS NOT NULL) AS used
                FROM gift_codes
                GROUP BY value
                ORDER BY value
                """
            )
        ).fetchall()

        data = [
            {
                "value": row.value,
                "total": row.total,
                "unused": row.unused,
                "used": row.used,
            }
            for row in rows
        ]
        return data
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
    """
    db = SessionLocal()
    try:
        conditions = []
        params: Dict[str, Any] = {"limit": limit}

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
        rows = db.execute(query, params).fetchall()

        codes = [
            {
                "id": row.id,
                "code": row.code,
                "value": row.value,
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
      { "value": 100, "codes": "KOD1
KOD2
KOD3" }  # string
      lub
      { "value": 100, "codes": ["KOD1", "KOD2", "KOD3"] }  # lista

    Zachowanie:
      - duplikaty kodów w DB są pomijane (ON CONFLICT DO NOTHING)
      - duplikaty w payloadzie są usuwane
      - zwracamy ile realnie dodano i ile pominięto
    """
    try:
        value = int(payload.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Nieprawidłowy nominał")

    codes_raw = payload.get("codes") or ""

    # Obsługa obu formatów: string i lista
    if isinstance(codes_raw, str):
        codes_in = [c.strip() for c in codes_raw.splitlines() if c.strip()]
    elif isinstance(codes_raw, list):
        codes_in = [str(c).strip() for c in codes_raw if str(c).strip()]
    else:
        codes_in = []

    # deduplikacja z zachowaniem kolejności
    seen = set()
    codes: List[str] = []
    for c in codes_in:
        c = c.strip()
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        codes.append(c)

    if not codes:
        raise HTTPException(status_code=400, detail="Brak kodów do dodania")

    db = SessionLocal()
    try:
        inserted = 0
        skipped = 0

        stmt = text(
            """
            INSERT INTO gift_codes (code, value, order_id)
            VALUES (:code, :value, NULL)
            ON CONFLICT (code) DO NOTHING
            """
        )

        for code in codes:
            res = db.execute(stmt, {"code": code, "value": value})
            if res.rowcount == 1:
                inserted += 1
            else:
                skipped += 1

        db.commit()
        logger.info("Dodano %s nowych kodów dla nominału %s (pominięto duplikaty: %s)", inserted, value, skipped)

        return {
            "status": "ok",
            "added": inserted,
            "inserted": inserted,  # dla zgodności z frontendem
            "skipped": skipped,
            "requested": len(codes),
        }

    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Błąd podczas dodawania nowych kodów: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()


@app.post("/admin/api/codes/correct-value")
def admin_correct_codes_value(payload: Dict[str, Any]):
    """
    Korekta nominału dla wskazanych kodów.

    Wymaga:
      - newValue (int) – docelowy nominał
      - codes (string z kodami 1/linia lub lista)

    Zasady bezpieczeństwa:
      - modyfikujemy WYŁĄCZNIE kody nieprzypisane (order_id IS NULL)
      - kody przypisane są pomijane i raportowane w odpowiedzi
    """
    try:
        new_value = int(payload.get("newValue"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Nieprawidłowy docelowy nominał")

    codes_raw = payload.get("codes") or ""

    if isinstance(codes_raw, str):
        codes_in = [c.strip() for c in codes_raw.splitlines() if c.strip()]
    elif isinstance(codes_raw, list):
        codes_in = [str(c).strip() for c in codes_raw if str(c).strip()]
    else:
        codes_in = []

    # deduplikacja z zachowaniem kolejności
    seen = set()
    codes: List[str] = []
    for c in codes_in:
        c = c.strip()
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        codes.append(c)

    if not codes:
        raise HTTPException(status_code=400, detail="Brak kodów do korekty")

    db = SessionLocal()
    try:
        # pobierz stan dla podanych kodów
        # SQLAlchemy expanding param dla IN (...)
        from sqlalchemy import bindparam

        select_stmt = text(
            """
            SELECT code, value, order_id
            FROM gift_codes
            WHERE code IN :codes
            """
        ).bindparams(bindparam("codes", expanding=True))

        rows = db.execute(select_stmt, {"codes": codes}).fetchall()
        found_by_code = {r.code: {"value": r.value, "order_id": r.order_id} for r in rows}

        not_found = [c for c in codes if c not in found_by_code]
        assigned = [c for c, info in found_by_code.items() if info["order_id"] is not None]

        eligible = [c for c, info in found_by_code.items() if info["order_id"] is None]
        if eligible:
            update_stmt = text(
                """
                UPDATE gift_codes
                SET value = :new_value
                WHERE order_id IS NULL
                  AND code IN :codes
                """
            ).bindparams(bindparam("codes", expanding=True))

            res = db.execute(update_stmt, {"new_value": new_value, "codes": eligible})
            updated = int(res.rowcount or 0)
        else:
            updated = 0

        db.commit()

        return {
            "status": "ok",
            "requested": len(codes),
            "updated": updated,
            "skipped_assigned": len(assigned),
            "not_found": len(not_found),
            "assigned_codes": assigned[:50],  # ograniczamy payload
            "not_found_codes": not_found[:50],
        }

    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Błąd podczas korekty nominału: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()



# ------------------------------------------------------------------------------
# ADMIN API – ręczne przypisanie / wysyłka kart
# ------------------------------------------------------------------------------


@app.post("/admin/api/manual/issue")
def admin_manual_issue(payload: Dict[str, Any]):
    """
    Ręczne przypisanie (lub pobranie istniejącego) kodu karty do zamówienia.

    Wymaga:
      - value (int) – nominał
      - orderSerialNumber (str/int) – numer zamówienia (orderSerialNumber)
      - email (str) – adres e-mail do wysyłki (opcjonalnie do samego przypisania, ale UI go wymaga)

    Zasada:
      - jeśli orderSerialNumber ma już przypisany kod (jakikolwiek), zwracamy istniejący
      - jeśli nie, przypisujemy pierwszy wolny kod z puli dla danego nominału
      - jeśli przypisaliśmy NOWY kod i mamy skonfigurowany IdosellClient, aktualizujemy notatkę zamówienia
    """
    try:
        value = int(payload.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Nieprawidłowa wartość nominału")

    order_serial = payload.get("orderSerialNumber")
    if order_serial is None or str(order_serial).strip() == "":
        raise HTTPException(status_code=400, detail="Brak numeru zamówienia")

    email = (payload.get("email") or "").strip()
    order_serial_str = str(order_serial).strip()

    db = SessionLocal()
    try:
        # 1) Jeśli dla tego numeru zamówienia już jest przypisany kod – zwracamy go (zabezpieczenie przed duplikacją)
        existing = db.execute(
            text(
                """
                SELECT id, code, value, order_id
                FROM gift_codes
                WHERE order_id = :order_id
                ORDER BY id ASC
                LIMIT 1
                """
            ),
            {"order_id": order_serial_str},
        ).mappings().first()

        if existing:
            return {
                "status": "ok",
                "reused": True,
                "code": existing["code"],
                "value": int(existing["value"]),
                "orderSerialNumber": existing["order_id"],
                "email": email,
            }

        # 2) Przypisanie nowego kodu z puli (używamy tej samej logiki co webhook)
        code_obj = crud.assign_unused_gift_code(
            db,
            value=value,
            order_id=order_serial_str,
        )
        if not code_obj:
            raise HTTPException(status_code=409, detail=f"Brak dostępnych kodów dla nominału {value}")

        db.commit()

        assigned = {"code": code_obj.code, "value": int(code_obj.value)}
        note_updated = False

        # 3) Notatka w Idosell (po ręcznym przypisaniu)
        if idosell_client:
            note_text = f"Numer(y) karty podarunkowej: {assigned['code']} ({assigned['value']} zł)"
            try:
                idosell_client.update_order_note(order_serial_str, note_text)
                note_updated = True
            except IdosellApiError as e:
                logger.error(
                    "Błąd IdosellApiError przy aktualizacji notatki zamówienia %s: %s",
                    order_serial_str,
                    e,
                )
            except Exception as e:
                logger.exception(
                    "Nieoczekiwany błąd przy aktualizacji notatki zamówienia %s: %s",
                    order_serial_str,
                    e,
                )

        # 4) Log adminowy do webhook_logs (żeby było śladem)
        try:
            log_webhook_event(
                status="admin_manual_issue",
                message=f"Ręczne przypisanie kodu: {assigned['code']} ({assigned['value']} zł)",
                payload={"value": value, "orderSerialNumber": order_serial_str, "email": email},
                order_id=f"manual:{order_serial_str}",
                order_serial=order_serial_str,
            )
        except Exception:
            # log_webhook_event nie może zablokować panelu
            pass

        return {
            "status": "ok",
            "reused": False,
            "code": assigned["code"],
            "value": assigned["value"],
            "orderSerialNumber": order_serial_str,
            "email": email,
            "noteUpdated": note_updated,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Błąd ręcznego przypisania kodu: %s", e)
        raise HTTPException(status_code=500, detail="Błąd serwera")
    finally:
        db.close()


@app.get("/admin/api/manual/pdf")
def admin_manual_pdf(orderSerialNumber: str = Query(..., description="Numer zamówienia (orderSerialNumber)")):
    """
    Pobiera PDF dla kodu(ów) przypisanych do danego zamówienia.
    Jeśli jest >1 kod, zwraca ZIP z wieloma PDF-ami.
    """
    order_serial_str = str(orderSerialNumber).strip()
    if not order_serial_str:
        raise HTTPException(status_code=400, detail="Brak numeru zamówienia")

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT code, value
                FROM gift_codes
                WHERE order_id = :order_id
                ORDER BY id ASC
                """
            ),
            {"order_id": order_serial_str},
        ).mappings().all()

        if not rows:
            raise HTTPException(status_code=404, detail="Brak przypisanych kodów dla tego zamówienia")

        if len(rows) == 1:
            code_val = rows[0]
            pdf_bytes = generate_giftcard_pdf(code=str(code_val["code"]), value=int(code_val["value"]))
            filename = f"giftcard-{order_serial_str}-{code_val['value']}.pdf"
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        # wiele kodów => ZIP
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for i, cv in enumerate(rows, start=1):
                pdf_bytes = generate_giftcard_pdf(code=str(cv["code"]), value=int(cv["value"]))
                zf.writestr(f"giftcard-{order_serial_str}-{i}-{int(cv['value'])}.pdf", pdf_bytes)
        zbuf.seek(0)
        return Response(
            content=zbuf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="giftcards-{order_serial_str}.zip"'},
        )

    finally:
        db.close()


@app.post("/admin/api/manual/send-email")
def admin_manual_send_email(payload: Dict[str, Any]):
    """
    Wysyła e-mail do klienta z kodem(ami) przypisanymi do zamówienia.
    Opcjonalnie załącza PDF.
    """
    order_serial = payload.get("orderSerialNumber")
    email = (payload.get("email") or "").strip()
    attach_pdf = bool(payload.get("attachPdf"))

    if not order_serial or str(order_serial).strip() == "":
        raise HTTPException(status_code=400, detail="Brak numeru zamówienia")
    if not email:
        raise HTTPException(status_code=400, detail="Brak adresu e-mail")

    order_serial_str = str(order_serial).strip()

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT code, value
                FROM gift_codes
                WHERE order_id = :order_id
                ORDER BY id ASC
                """
            ),
            {"order_id": order_serial_str},
        ).mappings().all()

        if not rows:
            raise HTTPException(status_code=404, detail="Brak przypisanych kodów dla tego zamówienia")

        codes = [{"code": str(r["code"]), "value": int(r["value"])} for r in rows]

        if attach_pdf:
            attachments = []
            for c in codes:
                pdf_bytes = generate_giftcard_pdf(code=c["code"], value=c["value"])
                attachments.append((f"giftcard-{c['value']}.pdf", pdf_bytes))

            body_text = (
                "Dzień dobry,\n\n"
                f"W załączniku przesyłamy kartę podarunkową przypisaną do zamówienia {order_serial_str}.\n"
                "Kod(y):\n"
                + "\n".join([f"- {c['code']} ({c['value']} zł)" for c in codes])
                + "\n\nPozdrawiamy,\nWASSYL"
            )

            send_email(
                to_email=email,
                subject="WASSYL – Twoja karta podarunkowa",
                body_text=body_text,
                body_html=None,
                attachments=attachments,
            )
        else:
            # bez PDF – użyj produkcyjnego maila (szablon, formatowanie)
            send_giftcard_email(
                to_email=email,
                codes=codes,
                order_serial_number=order_serial_str,
            )

        try:
            log_webhook_event(
                status="admin_manual_email",
                message=f"Ręczna wysyłka e-mail (attachPdf={attach_pdf}) do {email}",
                payload={"orderSerialNumber": order_serial_str, "email": email, "attachPdf": attach_pdf, "codes": codes},
                order_id=f"manual:{order_serial_str}",
                order_serial=order_serial_str,
            )
        except Exception:
            pass

        return {"status": "ok", "sentTo": email, "attachPdf": attach_pdf, "codes": codes}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Błąd ręcznej wysyłki e-mail: %s", e)
        raise HTTPException(status_code=500, detail="Błąd serwera podczas wysyłki e-mail")
    finally:
        db.close()


@app.get("/admin/api/codes/export")
def admin_export_codes(
    value: Optional[int] = Query(None, description="Filtr po nominale (np. 100, 200)"),
    used: Optional[str] = Query(
        None, description="Filtr statusu: 'used' lub 'unused'"
    ),
):
    """
    Eksport kodów do pliku CSV (id;code;value;order_id).
    Respektuje te same filtry, co /admin/api/codes.
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
            ORDER BY id ASC
            """
        )
        rows = db.execute(query, params).fetchall()

        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["id", "code", "value", "order_id"])
        for row in rows:
            writer.writerow([row.id, row.code, row.value, row.order_id])

        csv_data = output.getvalue()
        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="gift_codes_export.csv"'
            },
        )
    except SQLAlchemyError as e:
        logger.exception("Błąd podczas eksportu kodów: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()


@app.get("/admin/api/logs")
def admin_list_logs(
    limit: int = Query(50, ge=1, le=200, description="Maksymalna liczba logów"),
):
    """
    Zwraca ostatnie logi webhooka z tabeli webhook_events.
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT id, event_type, status, message,
                       order_id, order_serial, created_at
                FROM webhook_events
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).fetchall()

        logs = []
        for row in rows:
            created_at = None
            if getattr(row, "created_at", None) is not None:
                try:
                    created_at = row.created_at.isoformat(sep=" ", timespec="seconds")
                except Exception:
                    created_at = str(row.created_at)
            logs.append(
                {
                    "id": row.id,
                    "event_type": row.event_type,
                    "status": row.status,
                    "message": row.message,
                    "order_id": row.order_id,
                    "order_serial": row.order_serial,
                    "created_at": created_at,
                }
            )
        return logs
    except SQLAlchemyError as e:
        logger.exception("Błąd podczas pobierania logów webhooka: %s", e)
        raise HTTPException(status_code=500, detail="Błąd bazy danych")
    finally:
        db.close()



