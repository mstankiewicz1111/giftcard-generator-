import os
import logging
import base64
import requests
from typing import List, Tuple, Dict

logger = logging.getLogger("giftcard-webhook")

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "kontakt@wowpr.pl")
SENDGRID_FROM_NAME = os.getenv("SENDGRID_FROM_NAME", "Wassyl")

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def _build_attachments(attachments: List[Tuple[str, bytes]]):
    """
    attachments: lista (filename, bytes)
    """
    result = []
    for filename, content in attachments:
        result.append({
            "content": base64.b64encode(content).decode("ascii"),
            "type": "application/pdf",
            "filename": filename,
            "disposition": "attachment",
        })
    return result


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    attachments: List[Tuple[str, bytes]] | None = None,
):
    """
    Ogólna funkcja do wysyłki maila przez SendGrid.
    attachments – lista (filename, bytes) lub None.
    """
    if not SENDGRID_API_KEY:
        raise RuntimeError("Brak SENDGRID_API_KEY w zmiennych środowiskowych")

    data: Dict = {
        "personalizations": [
            {"to": [{"email": to_email}]}
        ],
        "from": {
            "email": SENDGRID_FROM_EMAIL,
            "name": SENDGRID_FROM_NAME,
        },
        "subject": subject,
        "content": [
            {
                "type": "text/plain",
                "value": body_text,
            }
        ],
    }

    if attachments:
        data["attachments"] = _build_attachments(attachments)

    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }

    resp = requests.post(SENDGRID_API_URL, json=data, headers=headers, timeout=10)
    try:
        resp.raise_for_status()
    except Exception as e:
        logger.error("Błąd przy wysyłce maila SendGrid: %s, response=%s", e, resp.text)
        raise

    logger.info("Wysłano e-mail na %s (SendGrid)", to_email)


def send_giftcard_email(
    to_email: str,
    order_id: str,
    codes: List[Dict[str, str]],
    pdf_files: List[Tuple[str, bytes]],
):
    """
    Wysyła maila z kartami podarunkowymi w załącznikach.
    pdf_files: lista (filename, bytes)
    """
    subject = f"Twoja karta podarunkowa – zamówienie {order_id}"

    lines = [
        "Dziękujemy za zakup karty podarunkowej w sklepie Wassyl!",
        "",
        "W załączniku znajdziesz swoje karty w formacie PDF.",
        "",
        "Podsumowanie kart:",
    ]
    for c in codes:
        lines.append(f"- {c['value']} zł – kod: {c['code']}")
    lines.append("")
    lines.append("Miłych zakupów!")
    body_text = "\n".join(lines)

    send_email(
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        attachments=pdf_files,
    )
