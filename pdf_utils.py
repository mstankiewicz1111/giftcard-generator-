import io
import os

from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Szablon karty
TEMPLATE_PATH = os.path.join(BASE_DIR, "WASSYL-GIFTCARD.pdf")

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


def generate_giftcard_pdf(code: str, value: int) -> bytes:
    """
    Generuje pojedynczą kartę podarunkową jako PDF.
    """

    # 1. Wczytanie szablonu
    with open(TEMPLATE_PATH, "rb") as f:
        template_bytes = f.read()

    template_reader = PdfReader(io.BytesIO(template_bytes))
    base_page = template_reader.pages[0]

    width = float(base_page.mediabox.width)
    height = float(base_page.mediabox.height)

    # 2. Przygotowanie nakładki
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(width, height))

    value_font, code_font = _get_font_names()

    # --- POZYCJE TEKSTU (lewy dół to 0,0) ---
    # trochę „na oko”, potem można tylko delikatnie korygować wartości procentowe

    # Wysokość: bliżej dołu
    value_y = height * 0.235   # w białym polu, przy „Wartość:”
    code_y = height * 0.175    # wyraźnie niżej, przy „Numer karty:”

    # Szerokość: bardziej w prawo
    value_x = width * 0.40
    code_x = width * 0.34

    # --- Teksty ---
    value_text = f"{value} zł"
    code_text = str(code)

    # jeśli nie mamy DejaVuSans (fallback na Helvetica) – usuwamy ł,
    # żeby nie było pustego znaku
    if value_font == "Helvetica":
        value_text = value_text.replace("ł", "l").replace("Ł", "L")

    # 3. Rysowanie tekstu

    # Wartość – średni font
    c.setFont(value_font, 22)
    c.drawString(value_x, value_y, value_text)

    # Numer karty – mniejszy font i niżej
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
