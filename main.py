from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def root():
    return {"message": "GiftCard backend działa!"}

@app.post("/webhook/order")
async def webhook_order(request: Request):
    payload = await request.json()

    # W tym miejscu będzie logika:
    # 1. weryfikacja payloadu
    # 2. sprawdzenie czy zamówienie zawiera giftcard
    # 3. pobranie kodu z puli
    # 4. generowanie PDF
    # 5. wysłanie maila
    # 6. odesłanie info do sklepu
    print("Webhook odebrany:", payload)

    return {"status": "ok"}
