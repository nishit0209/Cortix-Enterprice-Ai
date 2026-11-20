"""Extraction per format with synthetic files generated in the test (no DB, no network)."""

from __future__ import annotations

import pytest

from apps.documents import extraction
from apps.documents.exceptions import ExtractionError
from apps.documents.extraction import OcrResult, extract

from . import synth


def _write(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


@pytest.fixture
def no_tesseract(monkeypatch):
    monkeypatch.setattr(extraction, "tesseract_languages", lambda: None)


def test_pdf_text_headings_and_pages(tmp_path, no_tesseract):
    data = synth.pdf_bytes([
        ["Quarterly Sales Report", "Revenue grew in the North region during the first quarter of 2026.",
         "The South region was flat compared with the previous quarter."],
        ["Recommendations", "Increase inventory in the North region before the festive season."],
    ])
    out = extract(_write(tmp_path, "r.pdf", data), "pdf")
    assert [p.number for p in out.pages] == [1, 2]
    assert all(p.method == "text" for p in out.pages)
    assert "North region" in out.pages[0].text
    headings = [b.text for b in out.pages[0].blocks if b.kind == "heading"]
    assert headings and headings[0] == "Quarterly Sales Report"
    assert any(b.kind == "heading" and b.text == "Recommendations" for b in out.pages[1].blocks)


def test_pdf_tables_are_extracted(tmp_path, no_tesseract):
    data = synth.pdf_bytes([["Regional Table", "Units and revenue by region for the quarter."]], table_on=1)
    out = extract(_write(tmp_path, "t.pdf", data), "pdf")
    tables = out.pages[0].tables
    assert tables, "the ruled table should be detected by pdfplumber"
    assert tables[0].header == ["Region", "Units", "Revenue"]
    assert ["North", "120", "45000"] in tables[0].rows


def test_scanned_page_without_tesseract_is_marked_unavailable(tmp_path, no_tesseract):
    data = synth.pdf_bytes([["Cover Page", "This page has a normal text layer for the test."], []],
                           blank_pages=(2,))
    out = extract(_write(tmp_path, "s.pdf", data), "pdf")
    assert out.pages[0].method == "text"
    assert out.pages[1].method == "ocr_unavailable" and out.pages[1].text == ""


def test_scanned_page_goes_through_ocr(tmp_path, monkeypatch):
    calls = []

    def fake_ocr(page):
        calls.append(page.page_number)
        return OcrResult("SCANNED RECEIPT\nTotal paid Rs. 4,500 on 12/02/2026", "ocr", 87.5)

    data = synth.pdf_bytes([["Cover Page", "This page has a normal text layer for the test."], []],
                           blank_pages=(2,))
    out = extraction.extract_pdf(_write(tmp_path, "o.pdf", data), max_pages=10, ocr=fake_ocr)
    assert calls == [2]
    assert out.pages[1].method == "ocr" and out.pages[1].ocr_confidence == 87.5
    assert "Total paid" in out.pages[1].text


def test_ocr_image_without_tesseract_never_crashes(no_tesseract):
    from PIL import Image

    result = extraction.ocr_image(Image.new("RGB", (50, 50), "white"))
    assert result.method == "ocr_unavailable" and result.text == ""


def test_ocr_preprocessing_binarizes():
    from PIL import Image

    img = Image.new("RGB", (20, 20), (200, 200, 200))
    img.paste((20, 20, 20), (5, 5, 15, 15))
    out = extraction.preprocess_for_ocr(img)
    assert set(out.getdata()) <= {0, 255}


def test_pdf_page_limit(tmp_path, no_tesseract):
    data = synth.pdf_bytes([["Page one heading", "text " * 10], ["Page two heading", "text " * 10]])
    with pytest.raises(ExtractionError) as exc:
        extract(_write(tmp_path, "big.pdf", data), "pdf", max_pages=1)
    assert exc.value.code == "document_too_many_pages"


def test_corrupt_pdf_falls_back_to_pypdf_and_fails_cleanly(tmp_path):
    with pytest.raises(ExtractionError) as exc:
        extract(_write(tmp_path, "bad.pdf", b"%PDF-1.4\nthis is not really a pdf\n%%EOF"), "pdf")
    assert exc.value.code in ("document_corrupt", "document_empty")


def test_pypdf_fallback_reads_text(tmp_path):
    data = synth.pdf_bytes([["Fallback Heading", "Plain text extracted by pypdf for the fallback path."]])
    out = extraction.extract_pdf_pypdf(_write(tmp_path, "f.pdf", data), max_pages=5)
    assert "fallback path" in out.pages[0].text and out.warnings


def test_docx_headings_tables_and_page_breaks(tmp_path):
    out = extract(_write(tmp_path, "c.docx", synth.docx_bytes()), "docx")
    assert len(out.pages) == 2
    first = out.pages[0]
    assert [b.text for b in first.blocks if b.kind == "heading"] == ["Master Services Agreement", "Termination"]
    assert first.tables and first.tables[0].header == ["Milestone", "Fee"]
    assert any(b.kind == "table" for b in first.blocks)
    assert out.pages[1].blocks[0].text == "Confidentiality"


def test_docx_hindi_text(tmp_path):
    out = extract(_write(tmp_path, "h.docx", synth.docx_bytes(hindi=True)), "docx")
    assert synth.HINDI.split("।")[0] in out.pages[-1].text


def test_xlsx_sheets_become_pages(tmp_path):
    out = extract(_write(tmp_path, "s.xlsx", synth.xlsx_bytes()), "xlsx")
    assert [p.label for p in out.pages] == ["Sales", "Targets"]
    sales = out.pages[0].tables[0]
    assert sales.header == ["region", "month", "revenue"] and sales.rows[0] == ["North", "2026-01", "45000"]
    assert "North | 2026-01 | 45000" in out.pages[0].text


def test_csv_dialect_sniffing(tmp_path):
    out = extract(_write(tmp_path, "d.csv", synth.csv_bytes()), "csv")
    table = out.pages[0].tables[0]
    assert table.header == ["region", "units", "revenue"] and len(table.rows) == 2


def test_txt_pages_headings_and_scripts(tmp_path):
    out = extract(_write(tmp_path, "r.txt", synth.txt_bytes()), "txt")
    assert len(out.pages) == 2
    assert [b.text for b in out.pages[0].blocks if b.kind == "heading"] == ["Quarterly Sales Report", "Findings"]
    assert "कृत्रिम" in out.pages[1].text and "કૃત્રિમ" in out.pages[1].text


def test_txt_utf16_and_cp1252_decoding(tmp_path):
    out = extract(_write(tmp_path, "u.txt", "Résumé of synthetic data".encode("utf-16")), "txt")
    assert out.pages[0].text == "Résumé of synthetic data"
    out = extract(_write(tmp_path, "w.txt", "Café menu".encode("cp1252")), "txt")
    assert out.pages[0].text == "Café menu"
