import io
import os

from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Szablon karty
TEMPLATE_PATH = os.path.join(BASE_DIR, "WASSYL-GIFTCARD2.pdf")

# Własna czcionka z polskimi znakami
FONT_PATH = os.path.join(BASE_DIR, "DejaVuSans.ttf")
FONT_NAME = "DejaVuSans"


def _get_font_names() -> tuple[str, str]:
    """
    Zwraca nazwy czcionek do użycia (value_font, code_font).
    Jeśli jest DejaVuSans.ttf – rejestrujemy ją i używamy.
    Jeśli nie – wracamy do Helvetica (ale zamieniamy ł -> l).
    """
    if os.path.exists(FONT_PATH):
        if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
        return FONT_NAME, FONT_NAME

    # fallback
    return "Helvetica", "Helvetica"


def generate_giftcard_pdf(code: str, value: int | float | str) -> bytes:
    """
    Generuje pojedynczą kartę podarunkową jako PDF.

    value może być int/float/str – próba zrzutowania na int.
    """
    # 0. Walidacja value
    try:
        numeric_value = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"Nieprawidłowa wartość nominalna karty: {value!r}")

    # 1. Sprawdzenie obecności szablonu
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(
            f"Brak pliku szablonu PDF: {TEMPLATE_PATH}. "
            "Upewnij się, że WASSYL-GIFTCARD2.pdf jest w katalogu aplikacji."
        )

    # 2. Wczytanie szablonu
    with open(TEMPLATE_PATH, "rb") as f:
        template_bytes = f.read()

    template_reader = PdfReader(io.BytesIO(template_bytes))
    base_page = template_reader.pages[0]

    width = float(base_page.mediabox.width)
    height = float(base_page.mediabox.height)

    # 3. Przygotowanie nakładki
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(width, height))

    value_font, code_font = _get_font_names()

    # --- POZYCJE TEKSTU (lewy dół to 0,0) ---
    # Możesz delikatnie dostroić te współrzędne pod swój szablon

    value_y = height * 0.490
    value_x = width  * 0.650

    code_y  = height * 0.390
    code_x  = width  * 0.340

    value_text = f"{numeric_value} zł"
    code_text = str(code)

    if value_font == "Helvetica":
        value_text = value_text.replace("ł", "l").replace("Ł", "L")

    # Wartość — font 18
    c.setFont(value_font, 18)
    c.drawString(value_x, value_y, value_text)

    # Kod — font 16
    c.setFont(code_font, 16)
    c.drawString(code_x, code_y, code_text)

    c.save()

    # 4. Połączenie nakładki z szablonem
    packet.seek(0)
    overlay_reader = PdfReader(packet)
    overlay_page = overlay_reader.pages[0]

    base_page.merge_page(overlay_page)

    writer = PdfWriter()
    writer.add_page(base_page)

    output_stream = io.BytesIO()
    writer.write(output_stream)
    return output_stream.getvalue()
