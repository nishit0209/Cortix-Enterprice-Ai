"""Upload safety checks: extension allow-list, magic bytes, text sanity and the xlsx zip-bomb guard."""

from __future__ import annotations

import os
import zipfile
from typing import BinaryIO

from .errors import IngestError

ALLOWED_EXTENSIONS: dict[str, str] = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".txt": "txt",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".json": "json",
    ".ndjson": "ndjson",
}
TEXT_KINDS = {"csv", "tsv", "txt", "json", "ndjson"}
EXCEL_KINDS = {"xlsx", "xls"}
CONTENT_TYPES = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "txt": "text/plain",
    "json": "application/json",
    "ndjson": "application/x-ndjson",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
}

ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
BINARY_MAGICS: tuple[bytes, ...] = (
    ZIP_MAGIC, b"PK\x05\x06", OLE2_MAGIC, b"%PDF", b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"MZ", b"\x7fELF",
    b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!", b"BZh", b"\xfd7zXZ", b"PAR1", b"SQLite format 3",
)
BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")

ZIP_MAX_RATIO = 100
ZIP_MAX_UNCOMPRESSED = 1024**3  # 1 GiB
ZIP_RATIO_MIN_BYTES = 1024**2  # ratios of tiny archives are harmless; only judge archives that expand > 1 MiB
ZIP_MAX_MEMBERS = 10_000
HEAD_BYTES = 8192


def extension_kind(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    kind = ALLOWED_EXTENSIONS.get(ext)
    if kind is None:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise IngestError("upload_type_not_allowed", f"File type '{ext or '(none)'}' is not allowed. Allowed: {allowed}.")
    return kind


def check_zip_bomb(fileobj: BinaryIO) -> None:
    """Reject archives whose declared expansion is abusive (ratio > 100 or > 1 GiB uncompressed)."""
    pos = fileobj.tell()
    try:
        fileobj.seek(0)
        with zipfile.ZipFile(fileobj) as zf:
            infos = zf.infolist()
            if len(infos) > ZIP_MAX_MEMBERS:
                raise IngestError("upload_zip_bomb", "The workbook contains too many parts.")
            total_uncompressed = sum(max(0, i.file_size) for i in infos)
            total_compressed = sum(max(0, i.compress_size) for i in infos)
            if total_uncompressed > ZIP_MAX_UNCOMPRESSED:
                raise IngestError("upload_zip_bomb", "The workbook expands to more than 1 GB.")
            if total_uncompressed > ZIP_RATIO_MIN_BYTES and total_uncompressed > ZIP_MAX_RATIO * max(1, total_compressed):
                raise IngestError("upload_zip_bomb", "The workbook's compression ratio is suspicious (zip bomb).")
            names = {i.filename for i in infos}
            if not any(n.startswith("xl/") for n in names) or "[Content_Types].xml" not in names:
                raise IngestError("upload_content_mismatch", "The file is a zip archive but not an Excel workbook.")
    except zipfile.BadZipFile as exc:
        raise IngestError("upload_content_mismatch", "The Excel file is corrupt or not a valid workbook.") from exc
    finally:
        fileobj.seek(pos)


def _looks_binary(head: bytes) -> bool:
    if head.startswith(BOMS[1:]):  # UTF-16 with BOM legitimately contains NUL bytes
        return False
    return b"\x00" in head


def detect_kind(filename: str, fileobj: BinaryIO) -> str:
    """Return the verified file kind or raise IngestError(upload_type_not_allowed|upload_content_mismatch|...)."""
    kind = extension_kind(filename)
    pos = fileobj.tell()
    fileobj.seek(0)
    head = fileobj.read(HEAD_BYTES)
    fileobj.seek(pos)
    if not head:
        raise IngestError("upload_empty", "The file is empty.")
    if kind in EXCEL_KINDS:
        if head.startswith(ZIP_MAGIC):
            check_zip_bomb(fileobj)
            return "xlsx"  # an .xls that is really OOXML is accepted as xlsx
        if head.startswith(OLE2_MAGIC):
            return "xls"
        raise IngestError("upload_content_mismatch", "The file content is not an Excel workbook.")
    # text kinds
    if any(head.startswith(m) for m in BINARY_MAGICS) or _looks_binary(head):
        raise IngestError("upload_content_mismatch", "The file content is binary, not text.")
    if kind in ("json", "ndjson"):
        text = head
        for bom in BOMS:
            if text.startswith(bom):
                text = text[len(bom):]
                break
        stripped = text.replace(b"\x00", b"").lstrip()
        if not stripped:
            raise IngestError("upload_empty", "The file contains only whitespace.")
        if stripped[:1] not in (b"[", b"{"):
            raise IngestError("upload_content_mismatch", "A JSON file must contain an array of objects or NDJSON.")
    return kind
