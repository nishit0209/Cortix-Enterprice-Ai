"""Upload safety: extension allow-list, magic bytes, OOXML zip-bomb / macro / DTD guard, binary-as-text."""

from __future__ import annotations

import io
import zipfile

import pytest

from apps.documents import filetypes
from apps.documents.exceptions import ExtractionError

from . import synth


def _kind(name: str, data: bytes) -> str:
    return filetypes.detect_kind(name, io.BytesIO(data))


def _code(name: str, data: bytes) -> str:
    with pytest.raises(ExtractionError) as exc:
        _kind(name, data)
    return exc.value.code


def test_valid_files_pass():
    assert _kind("a.pdf", synth.pdf_bytes([["Heading", "body text"]])) == "pdf"
    assert _kind("a.docx", synth.docx_bytes()) == "docx"
    assert _kind("a.xlsx", synth.xlsx_bytes()) == "xlsx"
    assert _kind("a.csv", synth.csv_bytes()) == "csv"
    assert _kind("a.txt", synth.txt_bytes()) == "txt"
    assert _kind("A.PDF", synth.pdf_bytes([["Heading", "x"]])) == "pdf"


@pytest.mark.parametrize("name", ["a.exe", "a.doc", "a.html", "a", "a.pdf.exe", "a.svg"])
def test_extension_not_allowed(name):
    assert _code(name, b"hello") == "upload_type_not_allowed"


def test_magic_mismatches():
    assert _code("a.pdf", b"MZ\x90\x00 not a pdf") == "upload_content_mismatch"
    assert _code("a.docx", b"%PDF-1.7 pretending") == "upload_content_mismatch"
    assert _code("a.txt", b"\x7fELF\x02\x01\x01binary") == "upload_content_mismatch"
    assert _code("a.csv", b"a,b\x00,c") == "upload_content_mismatch"
    assert _code("a.txt", b"") == "upload_empty"


def test_xlsx_renamed_as_docx_is_rejected():
    assert _code("a.docx", synth.xlsx_bytes()) == "upload_content_mismatch"


def test_zip_bomb_rejected():
    bomb = synth.zip_bytes({**synth.minimal_docx_members(), "word/media/pad.bin": b"\0" * (30 * 1024 * 1024)})
    assert _code("bomb.docx", bomb) == "upload_zip_bomb"


def test_too_many_members_rejected(monkeypatch):
    monkeypatch.setattr(filetypes, "ZIP_MAX_MEMBERS", 3)
    members = {**synth.minimal_docx_members(), "a.xml": b"<a/>", "b.xml": b"<b/>"}
    assert _code("many.docx", synth.zip_bytes(members)) == "upload_zip_bomb"


def test_macro_part_rejected():
    members = {**synth.minimal_docx_members(), "word/vbaProject.bin": b"\x01\x02"}
    assert _code("m.docx", synth.zip_bytes(members)) == "upload_macro_rejected"


def test_xml_entity_expansion_rejected():
    evil = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">&lol2;</w:document>')
    assert _code("e.docx", synth.zip_bytes(synth.minimal_docx_members(evil))) == "upload_xml_unsafe"


def test_external_entity_in_content_types_rejected():
    members = synth.minimal_docx_members()
    members["[Content_Types].xml"] = (b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                                      b"<Types>&x;</Types>")
    assert _code("x.docx", synth.zip_bytes(members)) == "upload_xml_unsafe"


def test_corrupt_zip_rejected():
    data = synth.docx_bytes()
    assert _code("c.docx", data[:200]) == "upload_content_mismatch"


def test_path_traversal_member_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in synth.minimal_docx_members().items():
            zf.writestr(name, data)
        zf.writestr("../evil.xml", b"<x/>")
    assert _code("p.docx", buf.getvalue()) == "upload_content_mismatch"


def test_text_with_bom_is_accepted():
    assert _kind("b.txt", b"\xef\xbb\xbfhello") == "txt"
    assert _kind("b.txt", "hello".encode("utf-16")) == "txt"


def test_max_upload_bytes_respects_both_settings(settings):
    settings.DOCUMENT_MAX_UPLOAD_BYTES = 10_000
    settings.MAX_UPLOAD_BYTES = 5_000
    assert filetypes.max_upload_bytes() == 5_000
    settings.MAX_UPLOAD_BYTES = 50_000
    assert filetypes.max_upload_bytes() == 10_000
