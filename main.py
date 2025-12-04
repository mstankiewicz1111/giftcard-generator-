from fastapi import FastAPI, Request
import logging

from sqlalchemy import inspect
from pydantic import BaseModel
from typing import Dict, List

from database.models import Base, GiftCode
from database.session import engine, SessionLocal
from database import crud

from pdf_utils import generate_giftcard_pdf  # import generatora PDF
from fastapi.responses import StreamingResponse
import io
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


# 🔍 endpoint debugowy – pokazuje nazwy tabel w bazie
@app.get("/debug/tables")
def list_tables():
    inspector = inspect(engine)
    return inspector.get_table_names()

@app.get("/debug/test-pdf")
def debug_test_pdf():
    # przykładowe dane
    test_code = "TEST-1234-ABCD"
    test_value = 300

    pdf_bytes = generate_giftcard_pdf(code=test_code, value=test_value)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="giftcard-test.pdf"'
        },
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
    gift_lines = []

    for p in products:
        product_id = p.get("productId")
        quantity = p.get("productQuantity", 1)
        name = p.get("productName")
        size = p.get("sizePanelName")  # np. "100 zł", "200 zł", "300 zł", "500 zł"

        if product_id == GIFT_PRODUCT_ID:
            value = SIZE_TO_VALUE.get(size)

            if value is None:
                logger.warning(
                    "Znaleziono produkt karty (ID=%s), ale nieznana wartość sizePanelName=%s",
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
    # 3. Pobranie kodów z puli
    # --------------------------------------
    db = SessionLocal()
    assigned_codes = []

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
                assigned_codes.append({"code": used.code, "value": used.value})
    finally:
        db.close()

    logger.info("Przypisane kody dla zamówienia %s: %s", order_id, assigned_codes)

    # --------------------------------------
    # 4. Generowanie PDF dla każdego kodu
    # --------------------------------------
    generated_pdfs = []
    for item in assigned_codes:
        pdf_bytes = generate_giftcard_pdf(
            code=item["code"],
            value=item["value"]
        )
        generated_pdfs.append({
            "value": item["value"],
            "code": item["code"],
            "pdf_size_bytes": len(pdf_bytes)
        })

    # (tu możesz podpiąć wysyłkę maila)

    return {
        "status": "giftcards_assigned",
        "orderId": order_id,
        "giftLines": gift_lines,
        "assignedCodes": assigned_codes,
        "generatedPDFs": generated_pdfs,
    }


# ===== ADMIN: dodawanie wielu pul kodów naraz =====

class AddPoolsRequest(BaseModel):
    # np. {"100": ["KOD100-1", "KOD100-2"], "200": ["KOD200-1", ...]}
    pools: Dict[int, List[str]]


@app.post("/admin/add-pools")
def add_pools(req: AddPoolsRequest):
    db = SessionLocal()
    total_added = 0
    details = []

    try:
        for value, codes in req.pools.items():
            added_for_value = 0
            for code in codes:
                gc = GiftCode(code=code, value=value)
                db.add(gc)
                added_for_value += 1
                total_added += 1
            details.append({"value": value, "added": added_for_value})

        db.commit()
    except Exception as e:
        db.rollback()
        return {"status": "error", "details": str(e)}
    finally:
        db.close()

    return {"status": "ok", "total_added": total_added, "details": details}

