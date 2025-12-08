from fastapi import FastAPI, Request, Query
from fastapi.responses import Response
import logging
import os
from typing import List, Dict

from database.models import Base
from database.session import engine, SessionLocal
from database import crud
from pdf_utils import generate_giftcard_pdf
from email_utils import send_giftcard_email, send_email
from idosell_client import IdosellClient, IdosellApiError

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


@app.get("/debug/test-email")
async def debug_test_email(to: str = Query(..., description="Adres odbiorcy")):
    """
    Testowy endpoint wysyłki email — wysyła testową kartę w PDF jako załącznik.
    """
    # generujemy testowy PDF z istniejącej funkcji
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
        return {"status": "error", "message": str(e)}

