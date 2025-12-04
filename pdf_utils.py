import io
from typing import Union

from reportlab.pdfgen import canvas
from PyPDF2 import PdfReader, PdfWriter

TEMPLATE_PATH = "templates/WASSYL-GIFTCARD.pdf"


def generate_giftcard_pdf(code: str, value: int, output_path: Union[str, None] = None) -> bytes:
    """
    Tworzy PDF z kartą podarunkową na bazie szablonu WASSYL-GIFTCARD.pdf.
    Zwraca bajty PDF (do wysyłki mailem), opcjonalnie zapisuje na dysk jeśli podasz output_path.
    """

    # 1. Wczytujemy szablon, żeby znać rozmiar strony
    with open(TEMPLATE_PATH, "rb") as f:
        template_reader = PdfReader(f)
        base_page = template_reader.pages[0]
        width = float(base_page.mediabox.width)
        height = float(base_page.mediabox.height)

    # 2. Tworzymy "nakładkę" z tekstem (wartość + kod)
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(width, height))

    # Ustal pozycje tekstów (punkty od lewej/dół).
    # Te wartości możesz później delikatnie doregulować "na oko".
    # Startowo przyjmijmy coś, co dobrze wygląda na tym layoucie:
    value_text = f"{value} zł"

    c.setFont("Helvetica-Bold", 14)
    # wartość – prawa strona białego prostokąta
    c.drawRightString(width - 20, 55, value_text)

    # numer karty – pod wartością
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(width - 20, 35, code)

    c.save()
    packet.seek(0)

    overlay_reader = PdfReader(packet)

    # 3. Łączymy szablon z nakładką
    base_page.merge_page(overlay_reader.pages[0])

    writer = PdfWriter()
    writer.add_page(base_page)

    output_bytes = io.BytesIO()
    writer.write(output_bytes)
    pdf_data = output_bytes.getvalue()

    # opcjonalnie zapis na dysk (przydatne do debugowania lokalnie)
    if output_path:
        with open(output_path, "wb") as f_out:
            f_out.write(pdf_data)

    return pdf_data
