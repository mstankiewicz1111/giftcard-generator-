from fastapi import FastAPI, Request
from fastapi.responses import Response
import logging
import os
from typing import List, Dict

from database.models import Base
from database.session import engine, SessionLocal
from database import crud
from pdf_utils import generate_giftcard_pdf
from email_utils import send_giftcard_email
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


@app.on_event("startup")
def on_startup():
    # Utworzy tabelę gift_codes, jeśli jeszcze nie istnieje
    Base.metadata.create_all(bind=engine)


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
        result = db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
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


def notify_idosell_about_codes(order_id: str, codes: List[Dict[str, str]]):
    """
    Placeholder do wysyłania informacji o przydzielonych kodach do Idosell.
    Wymaga uzupełnienia konkretnym endpointem i parametrami WebAPI Idosell.
    """
    api_url = os.getenv("IDOSELL_API_URL")
    api_key = os.getenv("IDOSELL_API_KEY")

    if not api_url or not api_key:
        logger.info(
            "Idosell API nie skonfigurowane (brak IDOSELL_API_URL/IDOSELL_API_KEY) – pomijam powiadomienie."
        )
        return

    payload = {
        "method": "giftcard_notify",
        "orderId": order_id,
        "cards": codes,  # np. [{"code": "...", "value": 300}, ...]
    }

    try:
        resp = requests.post(
            api_url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(
            "Powiadomiono Idosell o kodach dla zamówienia %s. Odpowiedź: %s",
            order_id,
            resp.text,
        )
    except Exception as e:
        logger.exception("Błąd przy wysyłaniu informacji do Idosell: %s", e)


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
    gift_lines = []

    for p in products:
        product_id = p.get("productId")
        quantity = p.get("productQuantity", 1)
        name = p.get("productName")
        size = p.get("sizePanelName")  # np. "100 zł", "200 zł", "300 zł"

        if product_id == GIFT_PRODUCT_ID:
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
    pdf_files = []
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
                codes=assigned_codes,
                pdf_files=pdf_files,
            )
        except Exception as e:
            logger.exception("Błąd przy wysyłaniu e-maila z kartą: %s", e)
    else:
        logger.warning(
            "Brak e-maila klienta lub brak przypisanych kodów dla zamówienia %s – pomijam wysyłkę maila.",
            order_id,
        )

    # --------------------------------------
    # 6. Powiadomienie Idosell o użytych kodach (placeholder)
    # --------------------------------------
    if assigned_codes:
        notify_idosell_about_codes(order_id, assigned_codes)

    # Odpowiedź webhooka
    return {
        "status": "giftcards_assigned",
        "orderId": order_id,
        "giftLines": gift_lines,
        "assignedCodes": assigned_codes,
    }
