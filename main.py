from fastapi import FastAPI, Request
import logging

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
}

@app.get("/")
def root():
    return {"message": "GiftCard backend działa!"}

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
            order_id, order_serial
        )
        return {"status": "not_paid", "orderId": order_id}

    logger.info(
        "Odebrano OPŁACONE zamówienie: orderId=%s, serial=%s, email=%s",
        order_id, order_serial, client_email
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
                    "Znaleziono produkt karty (ID=%s), ale nieznana wartość sizePanelName=%s",
                    product_id, size
                )
                continue

            gift_lines.append({
                "product_id": product_id,
                "quantity": quantity,
                "name": name,
                "size": size,
                "value": value,
            })

    if not gift_lines:
        logger.info(
            "Opłacone zamówienie %s nie zawiera kart podarunkowych – ignoruję.",
            order_id
        )
        return {"status": "no_giftcards", "orderId": order_id}

    logger.info("Zamówienie %s zawiera karty: %s", order_id, gift_lines)

    # --------------------------------------
    # 3. W TYM MIEJSCU: dalsza logika
    # --------------------------------------
    # - pobranie kodów z puli (wg value)
    # - generowanie PDF
    # - wysyłka maila do client_email
    # - dopisanie kodów do zamówienia przez API Idosell

    return {
        "status": "giftcards_detected",
        "orderId": order_id,
        "giftLines": gift_lines
    }
