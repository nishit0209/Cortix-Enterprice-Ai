"""Generate small SYNTHETIC documents (pdf, docx, xlsx, csv, txt) in tests. No real data: every name, number and
identifier below is invented (PAN/GSTIN/Aadhaar-like values are format-valid fakes)."""

from __future__ import annotations

import io
import zipfile

HINDI = "यह एक कृत्रिम परीक्षण दस्तावेज़ है। इसमें बिक्री रिपोर्ट का सारांश है। कुल राजस्व ₹ 5 लाख रहा।"
GUJARATI = "આ એક કૃત્રિમ પરીક્ષણ દસ્તાવેજ છે. તેમાં વેચાણ અહેવાલનો સારાંશ છે. કુલ આવક સારી રહી."

INVOICE_TEXT = (
    "TAX INVOICE\n"
    "Invoice No: INV-2026-0042\n"
    "Bill To: Acme Synthetic Traders Pvt Ltd\n"
    "GSTIN: 27ABCDE1234F1Z5\n"
    "Contact: billing@acme-synthetic.example, +91 98765 43210\n"
    "Invoice Date: 15/03/2026  Due Date: 2026-04-14\n"
    "Subtotal: Rs. 1,20,000.00  CGST 9%  SGST 9%\n"
    "Amount Due: ₹ 1,41,600.00 (approx USD 1,700)\n"
)


def pdf_bytes(pages: list[list[str]], *, blank_pages: tuple[int, ...] = (), table_on: int | None = None) -> bytes:
    """Each page is a list of lines; the first line of each page is drawn larger (a heading). Pages listed in
    `blank_pages` (1-based) get no text layer (simulating a scanned page)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    for number, lines in enumerate(pages, start=1):
        if number not in blank_pages:
            y = height - 72
            for i, line in enumerate(lines):
                c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 18 if i == 0 else 11)
                c.drawString(72, y, line)
                y -= 28 if i == 0 else 16
            if table_on == number:
                _draw_table(c, 72, y - 20)
        else:
            c.rect(100, 400, 200, 100, fill=1)  # an "image" area, no text
        c.showPage()
    c.save()
    return buf.getvalue()


def _draw_table(c, x, y):
    rows = [["Region", "Units", "Revenue"], ["North", "120", "45000"], ["South", "95", "38000"]]
    col_w, row_h = 100, 20
    for r, row in enumerate(rows):
        for col, value in enumerate(row):
            c.rect(x + col * col_w, y - r * row_h, col_w, row_h)
            c.drawString(x + col * col_w + 4, y - r * row_h + 6, value)


def docx_bytes(*, hindi: bool = False) -> bytes:
    import docx
    from docx.enum.text import WD_BREAK

    d = docx.Document()
    d.add_heading("Master Services Agreement", level=1)
    d.add_paragraph("This Agreement is made between Nimbus Synthetic Labs LLP and Orbit Example Systems "
                    "Private Limited, hereinafter referred to as the parties.")
    d.add_heading("Termination", level=2)
    d.add_paragraph("Either party may terminate this agreement with 30 days notice. The governing law is India.")
    table = d.add_table(rows=3, cols=2)
    for r, (a, b) in enumerate([("Milestone", "Fee"), ("Design", "Rs. 50,000"), ("Build", "Rs. 1,50,000")]):
        table.cell(r, 0).text = a
        table.cell(r, 1).text = b
    p = d.add_paragraph("Signature page follows.")
    p.add_run().add_break(WD_BREAK.PAGE)
    d.add_heading("Confidentiality", level=2)
    d.add_paragraph(HINDI if hindi else "All information exchanged remains confidential for five years.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def xlsx_bytes() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["region", "month", "revenue"])
    ws.append(["North", "2026-01", 45000])
    ws.append(["South", "2026-01", 38000])
    ws2 = wb.create_sheet("Targets")
    ws2.append(["region", "target"])
    ws2.append(["North", 50000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes() -> bytes:
    return b"region;units;revenue\nNorth;120;45000\nSouth;95;38000\n"


def txt_bytes() -> bytes:
    body = (
        "# Quarterly Sales Report\n\nExecutive summary: revenue grew in the North region during Q1 2026.\n\n"
        "## Findings\n\nThe North region sold 120 units. The South region sold 95 units.\n"
        "\f"
        "# हिंदी सारांश\n\n" + HINDI + "\n\n# ગુજરાતી સારાંશ\n\n" + GUJARATI + "\n"
    )
    return body.encode("utf-8")


def zip_bytes(members: dict[str, bytes], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def minimal_docx_members(document_xml: bytes | None = None) -> dict[str, bytes]:
    return {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
                               b'2006/content-types"/>',
        "word/document.xml": document_xml or b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
                                             b'openxmlformats.org/wordprocessingml/2006/main"/>',
    }
