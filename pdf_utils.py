import io
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PyPDF2 import PdfReader, PdfWriter

# Nazwa pliku szablonu, który jest w repo (w katalogu głównym)
TEMPLATE_PATH = "GIFTCARD.pdf"


def generate_giftcard_pdf(code: str, value: int) -> bytes:
    """
    Generuje PDF karty podarunkowej na podstawie szablonu GIFTCARD.pdf.
    Na pierwszej stronie nakłada:
      - kod karty
      - wartość nominalną (np. 100 zł)
    Zwraca bajty pliku PDF.
    """

    # 1. Otwieramy szablon i NIE zamykamy go, dopóki nie skończymy merge'owania.
    with open(TEMPLATE_PATH, "rb") as template_file:
        base_reader = PdfReader(template_file)
        base_page = base_reader.pages[0]

        # 2. Tworzymy nakładkę (overlay) z tekstem
        packet = io.BytesIO()

        # A4 w reportlab to (595.27, 841.89) – ale ważne jest tylko,
        # że używamy tego samego formatu co szablon.
        c = canvas.Canvas(packet, pagesize=A4)

        # TODO: dostosuj współrzędne po pierwszym podglądzie
        # (później po prostu przesuniemy X/Y, jeśli będzie trzeba)
        CODE_X, CODE_Y = 300, 320   # pozycja kodu
        VALUE_X, VALUE_Y = 300, 280  # pozycja wartości

        # Kod karty
        c.setFont("Helvetica-Bold", 18)
        c.drawString(CODE_X, CODE_Y, code)

        # Wartość karty (np. "300 zł")
        c.setFont("Helvetica-Bold", 22)
        c.drawString(VALUE_X, VALUE_Y, f"{value} zł")

        c.save()

        # 3. Przewijamy bufor z overlayem na początek i wczytujemy jako PDF
        packet.seek(0)
        overlay_reader = PdfReader(packet)
        overlay_page = overlay_reader.pages[0]

        # 4. Łączymy pierwszą stronę szablonu z pierwszą stroną nakładki
        base_page.merge_page(overlay_page)

        # 5. Zapisujemy wynik do nowego PDF-a w pamięci
        writer = PdfWriter()
        writer.add_page(base_page)

        output_buffer = io.BytesIO()
        writer.write(output_buffer)

        return output_buffer.getvalue()
