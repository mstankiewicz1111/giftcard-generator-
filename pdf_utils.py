import io
from reportlab.pdfgen import canvas
from PyPDF2 import PdfReader, PdfWriter

TEMPLATE_PATH = "GIFTCARD.pdf"

# Rozmiar strony Twojej karty (odczytany z PDF)
TEMPLATE_WIDTH = 240.75
TEMPLATE_HEIGHT = 161.04


def generate_giftcard_pdf(code: str, value: int) -> bytes:
    """
    Generuje PDF karty podarunkowej na podstawie szablonu GIFTCARD.pdf.
    Na pierwszej stronie:
      - pozostawia grafikę szablonu,
      - dokłada kod i wartość karty na środku.
    Zwraca bajty gotowego PDF-a.
    """

    # 1. Wczytujemy szablon do pamięci (bez zostawiania otwartego pliku)
    with open(TEMPLATE_PATH, "rb") as f:
        template_bytes = f.read()

    template_stream = io.BytesIO(template_bytes)
    base_reader = PdfReader(template_stream)
    base_page = base_reader.pages[0]

    # 2. Tworzymy nakładkę o takim samym rozmiarze jak karta
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(TEMPLATE_WIDTH, TEMPLATE_HEIGHT))

    center_x = TEMPLATE_WIDTH / 2

    # Współrzędne dobrane „na oko” – tekst mniej więcej w środku.
    # Potem można je lekko skorygować po obejrzeniu pierwszego podglądu.
    CODE_Y = 90   # wyżej
    VALUE_Y = 60  # niżej

    # Kod karty (np. ABCD-1234-XYZ)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(center_x, CODE_Y, code)

    # Wartość karty (np. "300 zł")
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(center_x, VALUE_Y, f"{value} zł")

    c.save()
    packet.seek(0)

    overlay_reader = PdfReader(packet)
    overlay_page = overlay_reader.pages[0]

    # 3. Łączymy zawartość strony szablonu z nakładką
    base_page.merge_page(overlay_page)

    # 4. Zapisujemy wynik do nowego PDF-a
    writer = PdfWriter()
    writer.add_page(base_page)

    output = io.BytesIO()
    writer.write(output)

    return output.getvalue()
