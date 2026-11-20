"""Upload safety for documents: extension allow-list, magic bytes, text sanity and an OOXML (docx/xlsx) guard
against zip bombs, macro parts and XML DTD/entity tricks (parsed with defusedxml)."""

from __future__ import annotations

import os
import zipfile
from typing import BinaryIO

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from django.conf import settings

from .exceptions import ExtractionError

ALLOWED_EXTENSIONS: dict[str, str] = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".csv": "csv", ".txt": "txt"}
CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "txt": "text/plain",
}
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
HEAD_BYTES = 8192

ZIP_MAGIC = b"PK\x03\x04"
BINARY_MAGICS: tuple[bytes, ...] = (
    ZIP_MAGIC, b"PK\x05\x06", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"%PDF", b"\x89PNG", b"GIF8", b"\xff\xd8\xff",
    b"MZ", b"\x7fELF", b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!", b"BZh", b"\xfd7zXZ", b"PAR1", b"SQLite format 3",
)
TEXT_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")

ZIP_MAX_MEMBERS = 5_000
ZIP_MAX_UNCOMPRESSED = 512 * 1024**2
ZIP_MAX_RATIO = 100
ZIP_RATIO_MIN_BYTES = 1024**2
XML_PROLOG_SCAN = 4096
OOXML_MAIN_PART = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}
MACRO_PARTS = ("vbaproject.bin", "vbadata.xml")


def max_upload_bytes() -> int:
    cap = int(getattr(settings, "DOCUMENT_MAX_UPLOAD_BYTES", DEFAULT_MAX_BYTES))
    return min(cap, int(getattr(settings, "MAX_UPLOAD_BYTES", cap)))


def extension_kind(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    kind = ALLOWED_EXTENSIONS.get(ext)
    if kind is None:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ExtractionError("upload_type_not_allowed",
                              f"File type '{ext or '(none)'}' is not allowed. Allowed: {allowed}.")
    return kind


def check_ooxml(fileobj: BinaryIO, kind: str) -> None:
    """Zip-bomb / structure / macro / DTD guard for docx and xlsx. Raises ExtractionError."""
    pos = fileobj.tell()
    try:
        fileobj.seek(0)
        with zipfile.ZipFile(fileobj) as zf:
            infos = zf.infolist()
            if len(infos) > ZIP_MAX_MEMBERS:
                raise ExtractionError("upload_zip_bomb", "The file contains too many parts.")
            total_uncompressed = sum(max(0, i.file_size) for i in infos)
            total_compressed = sum(max(0, i.compress_size) for i in infos)
            if total_uncompressed > ZIP_MAX_UNCOMPRESSED:
                raise ExtractionError("upload_zip_bomb", "The file expands to more than 512 MB.")
            if (total_uncompressed > ZIP_RATIO_MIN_BYTES
                    and total_uncompressed > ZIP_MAX_RATIO * max(1, total_compressed)):
                raise ExtractionError("upload_zip_bomb", "The file's compression ratio is suspicious (zip bomb).")
            names = {i.filename for i in infos}
            lowered = {n.lower() for n in names}
            if "[Content_Types].xml" not in names or OOXML_MAIN_PART[kind] not in names:
                raise ExtractionError("upload_content_mismatch",
                                      f"The file is a zip archive but not a valid .{kind} document.")
            if any(n.rsplit("/", 1)[-1] in MACRO_PARTS for n in lowered):
                raise ExtractionError("upload_macro_rejected", "Documents containing macros are not accepted.")
            if any(".." in n.split("/") or n.startswith("/") for n in names):
                raise ExtractionError("upload_content_mismatch", "The archive contains unsafe paths.")
            try:
                SafeET.fromstring(zf.read("[Content_Types].xml"))
            except (DefusedXmlException, SafeET.ParseError) as exc:
                raise ExtractionError("upload_xml_unsafe", "The document's XML is malformed or unsafe.") from exc
            for info in infos:
                if not info.filename.lower().endswith((".xml", ".rels")):
                    continue
                with zf.open(info) as member:
                    head = member.read(XML_PROLOG_SCAN)
                if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                    raise ExtractionError("upload_xml_unsafe", "XML document type declarations are not allowed.")
    except zipfile.BadZipFile as exc:
        raise ExtractionError("upload_content_mismatch", f"The .{kind} file is corrupt.") from exc
    finally:
        fileobj.seek(pos)


def _check_text(head: bytes) -> None:
    if head.startswith(TEXT_BOMS):
        return
    if any(head.startswith(m) for m in BINARY_MAGICS) or b"\x00" in head:
        raise ExtractionError("upload_content_mismatch", "The file is binary, not text.")


def detect_kind(filename: str, fileobj: BinaryIO) -> str:
    """Extension + content checks. Returns the kind. Raises ExtractionError on any mismatch."""
    kind = extension_kind(filename)
    pos = fileobj.tell()
    fileobj.seek(0)
    head = fileobj.read(HEAD_BYTES)
    fileobj.seek(pos)
    if not head:
        raise ExtractionError("upload_empty", "The file is empty.")
    if kind == "pdf":
        if b"%PDF-" not in head[:1024]:
            raise ExtractionError("upload_content_mismatch", "The file does not look like a PDF.")
    elif kind in ("docx", "xlsx"):
        if not head.startswith(ZIP_MAGIC):
            raise ExtractionError("upload_content_mismatch", f"The file does not look like a .{kind} document.")
        check_ooxml(fileobj, kind)
    else:
        _check_text(head)
    return kind
