"""Text/structure extraction for pdf, docx, xlsx, csv and txt (AI_ARCHITECTURE.md §7.1). Pure functions over a
local file path: no database, no network.

* PDF: pdfplumber text lines (heading detection by font size) + tables per page; pypdf fallback when pdfplumber
  cannot open the file; pages with (almost) no text layer go to OCR.
* OCR: Tesseract via pytesseract with eng+hin+guj (whatever subset is installed), PIL preprocessing
  (grayscale, autocontrast, binarize). A missing tesseract binary marks the page `ocr_unavailable` (never
  crashes); per-page mean word confidence is kept.
* DOCX: python-docx paragraphs (Heading/Title styles -> headings), tables in body order, page numbers from
  rendered/explicit page breaks.
* XLSX: openpyxl (read-only, values only), one page per sheet rendered as a text table.
* CSV / TXT: decoded text; CSV is one table, TXT pages split on form feeds with heading heuristics.
"""

from __future__ import annotations

import csv
import io
import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

from .exceptions import ExtractionError
from .textutil import decode_text, heading_text, looks_like_heading, normalize_ws

log = logging.getLogger("nexora.documents")

MIN_TEXT_CHARS = 25          # fewer text-layer characters than this -> scanned page -> OCR
OCR_RESOLUTION = 300
OCR_LANGS = ("eng", "hin", "guj")
MAX_TABLE_ROWS = 2_000
MAX_TABLE_COLS = 60
MAX_SHEET_ROWS = 20_000
MAX_CELL_CHARS = 500
HEADING_SIZE_RATIO = 1.15


@dataclass
class Block:
    kind: str           # heading | paragraph | table
    text: str
    level: int = 0      # heading level (1 = top)


@dataclass
class TableData:
    header: list[str]
    rows: list[list[str]]
    title: str = ""
    truncated: bool = False
    total_rows: int = 0

    def as_text(self) -> str:
        lines = []
        if self.title:
            lines.append(self.title)
        if self.header:
            lines.append(" | ".join(self.header))
        lines.extend(" | ".join(r) for r in self.rows)
        return "\n".join(lines)


@dataclass
class PageContent:
    number: int
    text: str = ""
    method: str = "text"           # text | ocr | ocr_unavailable | ocr_failed | empty
    ocr_confidence: float | None = None
    blocks: list[Block] = field(default_factory=list)
    tables: list[TableData] = field(default_factory=list)
    label: str = ""
    language: str = ""


@dataclass
class Extracted:
    pages: list[PageContent]
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)


def _cell(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    return text[:MAX_CELL_CHARS]


def make_table(raw_rows, *, title: str = "", max_rows: int = MAX_TABLE_ROWS) -> TableData | None:
    rows: list[list[str]] = []
    total = 0
    for raw in raw_rows:
        row = [_cell(v) for v in list(raw)[:MAX_TABLE_COLS]]
        if not any(row):
            continue
        total += 1
        if len(rows) <= max_rows:
            rows.append(row)
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:max_rows + 1]
    return TableData(header=header, rows=body, title=title, truncated=total - 1 > len(body), total_rows=total - 1)


def blocks_from_lines(lines: list[str]) -> list[Block]:
    """Group plain-text lines into heading / paragraph blocks (blank lines and headings end paragraphs)."""
    blocks: list[Block] = []
    para: list[str] = []

    def flush():
        if para:
            blocks.append(Block("paragraph", " ".join(para).strip()))
            para.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            continue
        level = looks_like_heading(line)
        if level:
            flush()
            blocks.append(Block("heading", heading_text(line), level))
        else:
            para.append(line)
    flush()
    return blocks


# ------------------------------------------------------------------------------------------------- OCR
@dataclass
class OcrResult:
    text: str
    method: str
    confidence: float | None = None


@cache
def tesseract_languages() -> tuple[str, ...] | None:
    """Installed tesseract languages among eng/hin/guj, or None when the tesseract binary is unavailable."""
    try:
        import pytesseract

        available = set(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001 - TesseractNotFoundError, OSError, ... -> OCR unavailable
        return None
    langs = tuple(lang for lang in OCR_LANGS if lang in available)
    return langs or None


def preprocess_for_ocr(image):
    """Grayscale -> autocontrast -> global threshold (mean-based). OpenCV deskew is not available here."""
    from PIL import ImageOps

    gray = ImageOps.autocontrast(ImageOps.grayscale(image))
    hist = gray.histogram()
    total = sum(hist) or 1
    mean = sum(i * n for i, n in enumerate(hist)) / total
    threshold = max(90, min(200, int(mean * 0.85)))
    return gray.point(lambda p: 255 if p > threshold else 0, mode="1").convert("L")


def ocr_image(image) -> OcrResult:
    langs = tesseract_languages()
    if langs is None:
        return OcrResult("", "ocr_unavailable")
    try:
        import pytesseract

        data = pytesseract.image_to_data(preprocess_for_ocr(image), lang="+".join(langs),
                                         output_type=pytesseract.Output.DICT, timeout=120)
    except Exception as exc:  # noqa: BLE001 - a failed page must not fail the document
        log.warning("documents: OCR failed on a page: %s", type(exc).__name__)
        return OcrResult("", "ocr_failed")
    lines: dict[tuple, list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(data.get("text", [])):
        word = (word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (KeyError, ValueError, TypeError):
            conf = -1.0
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        confs.append(conf)
    text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
    return OcrResult(normalize_ws(text), "ocr", round(statistics.fmean(confs), 2) if confs else None)


# ------------------------------------------------------------------------------------------------- PDF
def _pdf_line_blocks(page) -> list[Block] | None:
    try:
        lines = page.extract_text_lines(return_chars=True, strip=True)
    except Exception:  # noqa: BLE001 - older pdfplumber / odd pages: fall back to text heuristics
        return None
    if not lines:
        return []
    sizes = []
    for ln in lines:
        chars = ln.get("chars") or []
        ln["_size"] = statistics.fmean(c.get("size", 0) for c in chars) if chars else 0.0
        sizes.extend(c.get("size", 0) for c in chars)
    body = statistics.median(sizes) if sizes else 0.0
    blocks: list[Block] = []
    para: list[str] = []
    prev_bottom = None

    def flush():
        if para:
            blocks.append(Block("paragraph", " ".join(para).strip()))
            para.clear()

    for ln in lines:
        text = (ln.get("text") or "").strip()
        if not text:
            continue
        big = body > 0 and ln["_size"] >= body * HEADING_SIZE_RATIO and len(text) <= 120
        level = 1 if big and ln["_size"] >= body * 1.5 else (2 if big else looks_like_heading(text))
        gap = prev_bottom is not None and (ln.get("top", 0) - prev_bottom) > max(4.0, body * 0.9)
        prev_bottom = ln.get("bottom", prev_bottom)
        if level:
            flush()
            blocks.append(Block("heading", heading_text(text), level))
            continue
        if gap:
            flush()
        para.append(text)
    flush()
    return blocks


def _pdf_tables(page) -> list[TableData]:
    out = []
    try:
        for raw in page.extract_tables() or []:
            table = make_table(raw)
            if table is not None and (table.rows or len(table.header) > 1):
                out.append(table)
    except Exception:  # noqa: BLE001 - table detection is best effort
        log.info("documents: table extraction failed on a page")
    return out


def _ocr_pdf_page(page) -> OcrResult:
    if tesseract_languages() is None:
        return OcrResult("", "ocr_unavailable")
    try:
        image = page.to_image(resolution=OCR_RESOLUTION).original
    except Exception:  # noqa: BLE001
        return OcrResult("", "ocr_failed")
    return ocr_image(image)


def extract_pdf(path: str, *, max_pages: int, ocr: Callable | None = None) -> Extracted:
    try:
        import pdfplumber

        pdf = pdfplumber.open(path)
    except Exception as exc:  # noqa: BLE001 - corrupt or unusual PDF: try pypdf
        log.info("documents: pdfplumber failed (%s); falling back to pypdf", type(exc).__name__)
        return extract_pdf_pypdf(path, max_pages=max_pages)
    ocr_fn = ocr or _ocr_pdf_page
    pages: list[PageContent] = []
    warnings: list[str] = []
    with pdf:
        if len(pdf.pages) > max_pages:
            raise ExtractionError("document_too_many_pages",
                                  f"The PDF has {len(pdf.pages)} pages; the limit is {max_pages}.")
        for i, page in enumerate(pdf.pages, start=1):
            try:
                text = normalize_ws(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                text = ""
                warnings.append(f"page {i}: text layer unreadable")
            pc = PageContent(number=i)
            if len(text) >= MIN_TEXT_CHARS:
                pc.text = text
                pc.blocks = _pdf_line_blocks(page) or blocks_from_lines(text.split("\n"))
                pc.tables = _pdf_tables(page)
            else:
                result = ocr_fn(page)
                pc.method = result.method
                pc.ocr_confidence = result.confidence
                pc.text = result.text or text
                if pc.text:
                    pc.blocks = blocks_from_lines(pc.text.split("\n"))
                elif result.method == "ocr":
                    pc.method = "empty"
            pages.append(pc)
    if not pages:
        raise ExtractionError("document_empty", "The PDF has no pages.")
    return Extracted(pages, warnings)


def extract_pdf_pypdf(path: str, *, max_pages: int) -> Extracted:
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ExtractionError("document_encrypted", "Password-protected PDFs are not supported.")
        n = len(reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("document_corrupt", "The PDF could not be read.") from exc
    if n > max_pages:
        raise ExtractionError("document_too_many_pages", f"The PDF has {n} pages; the limit is {max_pages}.")
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = normalize_ws(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            text = ""
        pc = PageContent(number=i, text=text, method="text" if text else "ocr_unavailable")
        pc.blocks = blocks_from_lines(text.split("\n")) if text else []
        pages.append(pc)
    if not pages:
        raise ExtractionError("document_empty", "The PDF has no pages.")
    return Extracted(pages, ["pdfplumber could not open the file; used pypdf (no tables, no OCR)"])


# ------------------------------------------------------------------------------------------------ DOCX
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_heading_level(paragraph) -> int:
    name = (getattr(paragraph.style, "name", "") or "").lower()
    if name == "title":
        return 1
    if name.startswith("heading"):
        digits = "".join(ch for ch in name if ch.isdigit())
        return min(3, int(digits)) if digits else 1
    return 0


def _page_breaks(element) -> int:
    n = 0
    for el in element.iter():
        if el.tag == f"{_W}lastRenderedPageBreak":
            n += 1
        elif el.tag == f"{_W}br" and el.get(f"{_W}type") == "page":
            n += 1
    return n


def extract_docx(path: str, *, max_pages: int) -> Extracted:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(path)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("document_corrupt", "The Word document could not be read.") from exc
    pages: dict[int, PageContent] = {}
    page_no = 1

    def page() -> PageContent:
        return pages.setdefault(page_no, PageContent(number=page_no))

    for child in document.element.body.iterchildren():
        if child.tag == f"{_W}p":
            para = Paragraph(child, document)
            breaks = _page_breaks(child)
            text = normalize_ws(para.text)
            if text:
                level = _docx_heading_level(para)
                page().blocks.append(Block("heading" if level else "paragraph", text, level))
            page_no += breaks
        elif child.tag == f"{_W}tbl":
            table = make_table([[c.text for c in row.cells] for row in Table(child, document).rows])
            if table is not None:
                page().tables.append(table)
                page().blocks.append(Block("table", table.as_text()))
            page_no += _page_breaks(child)
        if page_no > max_pages:
            raise ExtractionError("document_too_many_pages", f"The document exceeds {max_pages} pages.")
    out = []
    for n in sorted(pages):
        pc = pages[n]
        pc.text = "\n".join(b.text for b in pc.blocks)
        out.append(pc)
    if not out:
        out = [PageContent(number=1, method="empty")]
    return Extracted(out)


# ------------------------------------------------------------------------------------------------ XLSX
def extract_xlsx(path: str, *, max_pages: int) -> Extracted:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("document_corrupt", "The workbook could not be read.") from exc
    pages = []
    warnings = []
    try:
        for i, ws in enumerate(wb.worksheets, start=1):
            if i > max_pages:
                raise ExtractionError("document_too_many_pages", f"The workbook has more than {max_pages} sheets.")
            rows = []
            for r, row in enumerate(ws.iter_rows(values_only=True)):
                if r >= MAX_SHEET_ROWS:
                    warnings.append(f"sheet '{ws.title}': only the first {MAX_SHEET_ROWS} rows were read")
                    break
                rows.append(row)
            table = make_table(rows, title=ws.title)
            pc = PageContent(number=i, label=ws.title[:255])
            if table is None:
                pc.method = "empty"
            else:
                pc.tables = [table]
                pc.blocks = [Block("heading", ws.title, 1), Block("table", table.as_text())]
                pc.text = table.as_text()
            pages.append(pc)
    finally:
        wb.close()
    if not pages:
        raise ExtractionError("document_empty", "The workbook has no sheets.")
    return Extracted(pages, warnings)


# --------------------------------------------------------------------------------------------- CSV/TXT
def extract_csv(data: bytes) -> Extracted:
    text = decode_text(data)
    sample = text[:16_384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    table = make_table(reader)
    pc = PageContent(number=1)
    if table is None:
        pc.method = "empty"
    else:
        pc.tables = [table]
        pc.blocks = [Block("table", table.as_text())]
        pc.text = table.as_text()
    return Extracted([pc])


def extract_txt(data: bytes, *, max_pages: int) -> Extracted:
    text = decode_text(data)
    parts = text.split("\f")
    if len(parts) > max_pages:
        raise ExtractionError("document_too_many_pages", f"The text has more than {max_pages} pages.")
    pages = []
    for i, part in enumerate(parts, start=1):
        body = normalize_ws(part)
        pc = PageContent(number=i, text=body, method="text" if body else "empty")
        pc.blocks = blocks_from_lines(body.split("\n")) if body else []
        pages.append(pc)
    return Extracted(pages)


def extract(path: str, kind: str, *, max_pages: int = 500) -> Extracted:
    if kind == "pdf":
        return extract_pdf(path, max_pages=max_pages)
    if kind == "docx":
        return extract_docx(path, max_pages=max_pages)
    if kind == "xlsx":
        return extract_xlsx(path, max_pages=max_pages)
    with open(path, "rb") as fh:
        data = fh.read()
    if kind == "csv":
        return extract_csv(data)
    if kind == "txt":
        return extract_txt(data, max_pages=max_pages)
    raise ExtractionError("upload_type_not_allowed", f"Unsupported document kind {kind!r}.")
