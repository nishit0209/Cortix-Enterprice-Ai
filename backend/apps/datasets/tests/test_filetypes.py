"""Upload scanning: extension allow-list, magic bytes, binary detection, zip-bomb guard (no DB)."""

import io
import zipfile

import pytest

from apps.datasets.ingest import filetypes
from apps.datasets.ingest.errors import IngestError
from apps.datasets.tests.helpers import xlsx_bytes


def kind(name: str, data: bytes) -> str:
    return filetypes.detect_kind(name, io.BytesIO(data))


def code_of(name: str, data: bytes) -> str:
    with pytest.raises(IngestError) as exc:
        kind(name, data)
    return exc.value.code


@pytest.mark.parametrize(
    "name,data,expected",
    [
        ("a.csv", b"a,b\n1,2\n", "csv"),
        ("A.CSV", b"a,b\n1,2\n", "csv"),
        ("a.tsv", b"a\tb\n1\t2\n", "tsv"),
        ("a.txt", b"a|b\n1|2\n", "txt"),
        ("a.json", b'  [{"a": 1}]', "json"),
        ("a.ndjson", b'{"a": 1}\n{"a": 2}\n', "ndjson"),
        ("a.json", b"\xef\xbb\xbf[{}]", "json"),
        ("a.csv", "a,b\n1,2\n".encode("utf-16"), "csv"),  # UTF-16 with BOM contains NUL bytes legitimately
    ],
)
def test_accepts_valid_files(name, data, expected):
    assert kind(name, data) == expected


def test_xlsx_is_recognized_and_xls_with_ooxml_content_is_accepted_as_xlsx():
    data = xlsx_bytes([["a", "b"], [1, 2]])
    assert kind("book.xlsx", data) == "xlsx"
    assert kind("book.xls", data) == "xlsx"


def test_ole2_xls_magic_is_recognized():
    assert kind("old.xls", filetypes.OLE2_MAGIC + b"\x00" * 600) == "xls"


@pytest.mark.parametrize("name", ["a.exe", "a.parquet", "a", "a.csv.exe", "a.xlsm"])
def test_rejects_disallowed_extensions(name):
    assert code_of(name, b"a,b\n1,2\n") == "upload_type_not_allowed"


def test_rejects_empty_file():
    assert code_of("a.csv", b"") == "upload_empty"


@pytest.mark.parametrize(
    "name,data",
    [
        ("a.csv", b"%PDF-1.7 binary"),
        ("a.csv", b"\x89PNG\r\n\x1a\n...."),
        ("a.csv", b"MZ\x90\x00\x03"),
        ("a.csv", b"col\x00umn,b\n"),
        ("a.json", b"hello world"),
        ("a.xlsx", b"a,b\n1,2\n"),
        ("a.xls", b"plain text"),
    ],
)
def test_rejects_content_mismatch(name, data):
    assert code_of(name, data) == "upload_content_mismatch"


def test_rejects_zip_that_is_not_a_workbook():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    assert code_of("a.xlsx", buf.getvalue()) == "upload_content_mismatch"


def test_rejects_corrupt_zip():
    assert code_of("a.xlsx", filetypes.ZIP_MAGIC + b"garbage" * 100) == "upload_content_mismatch"


def test_zip_bomb_ratio_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", "0" * (20 * 1024 * 1024))  # 20 MB of zeros -> ratio >> 100
    data = buf.getvalue()
    assert len(data) < 200 * 1024
    assert code_of("bomb.xlsx", data) == "upload_zip_bomb"


def test_zip_bomb_declared_size_limit(monkeypatch):
    monkeypatch.setattr(filetypes, "ZIP_MAX_UNCOMPRESSED", 1000)
    data = xlsx_bytes([["a"], *[[i] for i in range(200)]])
    assert code_of("big.xlsx", data) == "upload_zip_bomb"
