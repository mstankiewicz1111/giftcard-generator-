import os
import base64
import requests

# Nadawca – bierzemy z env, jak dotąd
EMAIL_SENDER = os.getenv("SMTP_SENDER", "vouchery@wassyl.pl")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    attachments=None,  # lista (filename, bytes)
):
    """
    Wysyłka maila przez SendGrid API.
    attachments: lista krotek (filename, file_bytes) – np. PDF-y z kartą.
    """
    if not SENDGRID_API_KEY:
        raise RuntimeError("Brak SENDGRID_API_KEY w zmiennych środowiskowych")

    data = {
        "personalizations": [
            {
                "to": [{"email": to_email}],
            }
        ],
        "from": {"email": EMAIL_SENDER},
        "subject": subject,
        "content": [
            {
                "type": "text/plain",
                "value": body_text,
            }
        ],
    }

    if attachments:
        data["attachments"] = []
        for filename, file_bytes in attachments:
            data["attachments"].append(
                {
                    "content": base64.b64encode(file_bytes).decode("ascii"),
                    "filename": filename,
                    "type": "application/pdf",
                }
            )

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        json=data,
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}"},
        timeout=10,
    )
    resp.raise_for_status()


def send_giftcard_email(
    to_email: str,
    order_id: str,
    codes,
    pdf_files,
):
    """
    Wysyłka właściwego maila z kartami podarunkowymi + PDF-y w załączniku.
    codes: lista słowników {"code": "...", "value": 300}
    pdf_files: lista (filename, bytes)
    """
    lines = [
        "Dzień dobry,",
        "",
        "Dziękujemy za zakup karty podarunkowej WASSYL.",
        "",
        "Poniżej znajdują się dane kart:",
        "",
    ]
    for c in codes:
        lines.append(f"- {c['value']} zł, kod: {c['code']}")
    lines.extend(
        [
            "",
            "W załącznikach znajdziesz pliki PDF z kartami.",
            "",
            "Pozdrawiamy,",
            "Zespół WASSYL",
        ]
    )

    body_text = "\n".join(lines)

    send_email(
        to_email=to_email,
        subject=f"Karta podarunkowa WASSYL – zamówienie {order_id}",
        body_text=body_text,
        attachments=pdf_files,
    )
