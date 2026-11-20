"""Value rendering for CSV hand-off to ingestion (pure)."""

import datetime as dt
import uuid
from decimal import Decimal

import polars as pl

from apps.connectors.base import Batch, ColumnInfo
from apps.connectors.services import _batch_to_frame, write_frame_csv
from apps.connectors.values import to_text


def test_to_text():
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert to_text(None) is None
    assert to_text(True) == "true" and to_text(False) == "false"
    assert to_text(Decimal("12.50")) == "12.50"
    assert to_text(dt.datetime(2024, 1, 1, 15, 30, tzinfo=tz)) == "2024-01-01T10:00:00Z"
    assert to_text(dt.datetime(2024, 1, 1, 10, 0)) == "2024-01-01T10:00:00"
    assert to_text(dt.date(2024, 2, 29)) == "2024-02-29"
    assert to_text({"a": 1}) == '{"a": 1}' and to_text([1, "x"]) == '[1, "x"]'
    assert to_text(b"\x01\xff") == "\\x01ff"
    assert to_text(uuid.UUID(int=1)) == "00000000-0000-0000-0000-000000000001"
    assert to_text(float("nan")) == ""


def test_write_frame_csv_uses_ingest_friendly_formats(tmp_path):
    frame = pl.DataFrame({
        "ts": [dt.datetime(2024, 1, 1, 10, 0, tzinfo=dt.UTC), None],
        "d": [dt.date(2024, 1, 2), None],
        "b": [True, None],
        "n": [1.5, None],
        "s": ['a,"b"', None],
    })
    path = tmp_path / "out.csv"
    size = write_frame_csv(frame, str(path))
    text = path.read_text(encoding="utf-8")
    assert size == path.stat().st_size
    lines = text.splitlines()
    assert lines[0] == "ts,d,b,n,s"
    assert lines[1] == '2024-01-01T10:00:00Z,2024-01-02,true,1.5,"a,""b"""'
    assert lines[2] == ",,,,"


def test_batch_to_frame_is_text_typed():
    batch = Batch(columns=(ColumnInfo("id", "bigint"), ColumnInfo("total", "numeric")),
                  rows=[(1, Decimal("2.50")), (2, None)])
    frame = _batch_to_frame(batch)
    assert frame.schema == {"id": pl.String, "total": pl.String}
    assert frame.rows() == [("1", "2.50"), ("2", None)]
