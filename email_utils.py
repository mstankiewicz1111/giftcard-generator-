import os
import logging
import base64
from typing import List, Tuple, Dict, Optional

import requests

logger = logging.getLogger("giftcard-webhook")

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "kontakt@wowpr.pl")
SENDGRID_FROM_NAME = os.getenv("SENDGRID_FROM_NAME", "Wassyl")

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def _build_attachments(attachments: List[Tuple[str, bytes]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for filename, content in attachments:
        result.append(
            {
                "content": base64.b64encode(content).decode("ascii"),
                "type": "application/pdf",
                "filename": filename,
                "disposition": "attachment",
            }
        )
    return result


def send_email(
    to_email: str,
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
) -> None:
    if not SENDGRID_API_KEY:
        raise RuntimeError("Brak SENDGRID_API_KEY w zmiennych środowiskowych")

    content: List[Dict[str, str]] = []
    if body_text:
        content.append({"type": "text/plain", "value": body_text})
    if body_html:
        content.append({"type": "text/html", "value": body_html})

    if not content:
        raise ValueError("Musisz podać body_text lub body_html")

    data: Dict = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": SENDGRID_FROM_EMAIL, "name": SENDGRID_FROM_NAME},
        "subject": subject,
        "content": content,
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


def _build_giftcard_html(order_serial: str) -> str:
    """
    Tworzy finalny HTML maila — wklejony szablon + wstawiany numer orderSerialNumber.
    """

    return f'''<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8" />
  <title>Karta podarunkowa WASSYL</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto+Condensed:wght@400;700&display=swap');
    body {{ margin: 0; padding: 0; background-color: #ffffff; }}
    img {{ display: block; max-width: 100%; height: auto; }}
    table {{ border-collapse: collapse; }}
    .email-container {{ width: 960px; max-width: 960px; }}
    .body-text {{
        font-family: 'Roboto Condensed', Arial, sans-serif;
        font-size: 14px;
        line-height: 1.6;
        color: #000000;
    }}
    .section-divider {{ border-top: 1px solid #bfbfbf; }}
    .heading {{
        font-family: 'Roboto Condensed', Arial, sans-serif;
        font-size: 14px;
        font-weight: 700;
    }}
    @media only screen and (max-width: 600px) {{
        .email-container {{ width: 90% !important; }}
    }}
  </style>
</head>
<body>
  <table width="100%" cellspacing="0" cellpadding="0" align="center">
    <tr>
      <td align="center" style="padding: 24px 0;">
        <table class="email-container" cellspacing="0" cellpadding="0" align="center">

          <tr><td><div class="section-divider"></div></td></tr>

          <tr>
            <td align="center" style="padding: 24px 0 16px 0;">
              <img src="https://wassyl.pl/data/include/cms/gfx/logo-wassyl.png" alt="WASSYL" />
            </td>
          </tr>

          <tr><td><div class="section-divider"></div></td></tr>

          <tr>
            <td style="padding: 32px 0 8px 0;">
              <table width="100%">
                <tr>
                  <td class="body-text" style="padding: 0 32px;">
                    <p>Cześć!</p>
                    <p>
                        Dziękujemy za zakup naszej karty podarunkowej. W załączeniu tej wiadomości
                        znajdziesz plik w formacie PDF do samodzielnego wydruku karty.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr><td><div class="section-divider"></div></td></tr>

          <tr>
            <td style="padding: 24px 0;">
              <table width="100%">
                <tr>
                  <td class="body-text" style="padding: 0 32px;">
                    <p class="heading">Jak skorzystać z karty podarunkowej?</p>
                    <p>
                        Wystarczy, że podczas składania zamówienia w sklepie
                        <strong>WASSYL.pl</strong> wybierzesz jako metodę płatności opcję
                        „Karta podarunkowa”. W następnym kroku musisz po prostu podać numer karty.
                        Wartość zamówienia zostanie automatycznie pomniejszona. Jeśli nie wykorzystasz
                        wszystkich środków przypisanych do karty, możesz skorzystać z niej ponownie,
                        przy kolejnym zamówieniu.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr><td><div class="section-divider"></div></td></tr>

          <tr>
            <td style="padding: 24px 0 32px 0;">
              <table width="100%">
                <tr>
                  <td class="body-text" style="padding: 0 32px;">
                    <p>
                      Masz jakieś pytania? Coś nie zadziałało?
                      Pisz śmiało na
                      <a href="mailto:bok@wassyl.pl" style="font-weight:700; color:#000;">
                        bok@wassyl.pl
                      </a>!
                    </p>

                    <p>
                      W celu ułatwienia komunikacji z nami, podaj nam numer zamówienia
                      na zakup karty podarunkowej. W Twoim przypadku ten numer brzmi:
                      <strong>{order_serial}</strong>.
                    </p>

                    <p>Pozdrawiamy,</p>
                    <p><strong>zespół WASSYL.pl</strong></p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>'''


def send_giftcard_email(
    to_email: str,
    order_id: str,
    order_serial: str,
    codes: List[Dict[str, str]],
    pdf_files: List[Tuple[str, bytes]],
) -> None:
    """
    Wysyła maila z kartami podarunkowymi.
    UWAGA: dodano order_serial → wstawiany do HTML.
    """

    subject = f"Twoja karta podarunkowa – zamówienie {order_id}"

    # tekst jako fallback
    lines = [
        "Cześć!",
        "",
        "Dziękujemy za zakup naszej karty podarunkowej.",
        "W załączeniu przesyłamy PDF do samodzielnego wydruku.",
        "",
        "Podsumowanie kart:",
    ]
    for c in codes:
        lines.append(f"- {c['value']} zł – kod: {c['code']}")

    lines.extend(
        [
            "",
            "Jak skorzystać z karty?",
            "Wystarczy wybrać metodę płatności 'Karta podarunkowa' w sklepie WASSYL.pl "
            "i podać numer karty.",
            "",
            "W celu ułatwienia komunikacji podaj numer zamówienia:",
            f"Numer zamówienia: {order_serial}",
            "",
            "Pozdrawiamy, zespół WASSYL.pl",
        ]
    )

    body_text = "\n".join(lines)
    body_html = _build_giftcard_html(order_serial)

    send_email(
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=pdf_files,
    )
