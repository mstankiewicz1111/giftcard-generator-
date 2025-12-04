import io
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PyPDF2 import PdfReader, PdfWriter

TEMPLATE_PATH = "GIFTCARD.pdf"


def generate_giftcard_pdf(code: str, value: int) -> bytes:
    """
    Generuje PDF karty podarunkowej na podstawie szablonu GIFTCARD.pdf.
    """

    # --- 1. Wczytujemy CAŁY szablon do pamięci ---
    with open(TEMPLATE_PATH, "rb") as f:
        template_bytes = f.read()

    template_stream = io.BytesIO(template_bytes)
    base_reader = PdfReader(template_stream)
    base_page = base_reader.pages[0]

    # --- 2. Tworzymy nakładkę ---
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=A4)

    # Pozycje tekstu (potem dostroimy)
    CODE_X, CODE_Y = 300, 320
    VALUE_X, VALUE_Y = 300, 280

    c.setFont("Helvetica-Bold", 18)
    c.drawString(CODE_X, CODE_Y, code)

    c.setFont("Helvetica-Bold", 22)
    c.drawString(VALUE_X, VALUE_Y, f"{value} zł")

    c.save()
    packet.seek(0)

    overlay_reader = PdfReader(packet)
    overlay_page = overlay_reader.pages[0]

    # --- 3. Łączymy PDF w pamięci ---
    base_page.merge_page(overlay_page)

    writer = PdfWriter()
    writer.add_page(base_page)

    output = io.BytesIO()
    writer.write(output)

    return output.getvalue()
