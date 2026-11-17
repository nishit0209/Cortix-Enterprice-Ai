"""Parse uploaded files into an all-text Polars frame (types are inferred later by `inference`).

* Delimited text (csv/tsv/txt): encoding detection (BOM, strict UTF-8, charset-normalizer, UTF-8 fallback),
  delimiter + header sniffing, ragged-row detection.
* Excel (xlsx/xls): python-calamine (cached formula values, never evaluated), first non-empty sheet by default,
  merged two-row headers flattened.
* JSON: array of objects, wrapper object with one record list, or NDJSON. Nested objects are flattened to
  `parent__child` up to depth 3; deeper objects and arrays are kept as JSON text (loaded as jsonb).
"""

from __future__ import annotations

import codecs
import csv
import datetime as dt
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl
from django.conf import settings

from .errors import IngestError

ENCODING_SAMPLE_BYTES = 1024 * 1024
SNIFF_CHARS = 64 * 1024
DELIMITER_CANDIDATES = [",", ";", "\t", "|"]
JSON_MAX_DEPTH = 3


def max_columns() -> int:
    return int(getattr(settings, "DATASET_MAX_COLUMNS", 1000))


def max_rows() -> int:
    return int(getattr(settings, "DATASET_MAX_ROWS", 50_000_000))


@dataclass
class ParsedTable:
    frame: pl.DataFrame                       # all columns pl.String, positional names c0..cN
    source_names: list[str]                   # original header labels (may be empty/duplicated)
    json_columns: set[int] = field(default_factory=set)  # positions holding JSON text (-> jsonb)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return self.frame.height


@dataclass
class ParseOptions:
    sheet: str | None = None
    delimiter: str | None = None
    has_header: bool | None = None
    encoding: str | None = None


# ---------------------------------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------------------------------
def detect_encoding(sample: bytes) -> tuple[str, int]:
    """Return (python codec name, BOM length)."""
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8", 3
    if sample.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le", 4
    if sample.startswith(b"\xff\xfe"):
        return "utf-16-le", 2
    if sample.startswith(b"\xfe\xff"):
        return "utf-16-be", 2
    try:
        codecs.getincrementaldecoder("utf-8")("strict").decode(sample, final=False)
        return "utf-8", 0
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(sample).best()
        if best is not None and best.encoding:
            return codecs.lookup(best.encoding).name, 0
    except Exception:  # noqa: BLE001 - detection is best-effort; fall back to UTF-8 with replacement
        pass
    return "utf-8", 0


def _transcode_to_utf8(path: str, encoding: str, bom_len: int) -> str:
    """Write a UTF-8 copy of the file (BOM stripped, undecodable bytes replaced). Returns the new path."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    fd, out_path = tempfile.mkstemp(suffix=".utf8.csv")
    with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
        src.seek(bom_len)
        while True:
            block = src.read(1024 * 1024)
            if not block:
                dst.write(decoder.decode(b"", final=True).encode("utf-8"))
                break
            dst.write(decoder.decode(block).encode("utf-8"))
    return out_path


# ---------------------------------------------------------------------------------------------------
# delimited text
# ---------------------------------------------------------------------------------------------------
_QUOTED_RE = re.compile(r'"(?:[^"]|"")*"', re.DOTALL)


def sniff_delimiter(text: str, kind: str = "csv") -> str:
    if kind == "tsv":
        return "\t"
    unquoted = _QUOTED_RE.sub('""', text)
    lines = [ln for ln in unquoted.splitlines() if ln.strip()][:50]
    if len(lines) > 1:
        lines = lines[:-1] if not text.endswith(("\n", "\r")) else lines  # last line may be cut by the sample
    if not lines:
        return ","
    best, best_key = ",", (-1.0, 0, 0)
    for order, cand in enumerate(DELIMITER_CANDIDATES):
        counts = [ln.count(cand) for ln in lines]
        nonzero = [c for c in counts if c > 0]
        if not nonzero:
            continue
        mode = max(set(nonzero), key=nonzero.count)
        consistency = sum(1 for c in counts if c == mode) / len(counts)
        key = (consistency, mode, -order)
        if key > best_key:
            best, best_key = cand, key
    if best_key[0] < 0:
        try:
            return csv.Sniffer().sniff(text[:SNIFF_CHARS], delimiters="".join(DELIMITER_CANDIDATES)).delimiter
        except csv.Error:
            return ","
    return best


_NUMERIC_CELL_RE = re.compile(r"^\s*[-+]?(?:[₹$€£]\s*)?\d[\d,]*(?:\.\d+)?%?\s*$")
_DATE_CELL_RE = re.compile(r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")


def _is_data_like(cell: str | None) -> bool:
    if cell is None:
        return False
    return bool(_NUMERIC_CELL_RE.match(cell) or _DATE_CELL_RE.match(cell))


def sniff_header(rows: Sequence[Sequence[str | None]]) -> bool:
    """True if the first row is a header: it has fewer data-like cells than the rows below it."""
    if not rows:
        return True
    first = rows[0]
    if not any(c is not None and str(c).strip() for c in first):
        return False
    first_data = sum(_is_data_like(c) for c in first)
    if first_data == 0:
        return True
    if len(rows) < 2:
        return False
    body = rows[1 : min(len(rows), 21)]
    avg_body = sum(sum(_is_data_like(c) for c in r) for r in body) / len(body)
    return first_data < avg_body


def parse_delimited(path: str, kind: str = "csv", options: ParseOptions | None = None) -> ParsedTable:
    options = options or ParseOptions()
    with open(path, "rb") as fh:
        sample = fh.read(ENCODING_SAMPLE_BYTES)
    if not sample.strip():
        raise IngestError("upload_empty", "The file is empty.")
    if options.encoding:
        try:
            encoding, bom = codecs.lookup(options.encoding).name, 0
        except LookupError as exc:
            raise IngestError("invalid_option", f"Unknown encoding '{options.encoding}'.") from exc
    else:
        encoding, bom = detect_encoding(sample)
    utf8_path = path
    if encoding not in ("utf-8",) or bom:
        utf8_path = _transcode_to_utf8(path, encoding, bom)
    try:
        with open(utf8_path, "rb") as fh:
            text_sample = fh.read(SNIFF_CHARS * 2).decode("utf-8", errors="replace")
        delimiter = options.delimiter or sniff_delimiter(text_sample, kind)
        if len(delimiter) != 1:
            raise IngestError("invalid_option", "The delimiter must be a single character.")
        try:
            raw = pl.read_csv(
                utf8_path,
                has_header=False,
                separator=delimiter,
                quote_char='"',
                infer_schema=False,
                encoding="utf8-lossy",
                raise_if_empty=False,
                truncate_ragged_lines=False,
            )
        except pl.exceptions.ComputeError as exc:
            msg = str(exc)
            if "more fields" in msg or "ragged" in msg:
                raise IngestError(
                    "malformed_file", "Some rows have more fields than the header (check quoting and the delimiter)."
                ) from exc
            raise IngestError("malformed_file", "The file could not be parsed as delimited text.") from exc
        except pl.exceptions.PolarsError as exc:
            raise IngestError("malformed_file", "The file could not be parsed as delimited text.") from exc
    finally:
        if utf8_path != path:
            try:
                os.unlink(utf8_path)
            except OSError:
                pass
    if raw.width == 0 or raw.height == 0:
        raise IngestError("upload_empty", "The file has no rows.")
    head_rows = [list(r) for r in raw.head(21).iter_rows()]
    has_header = options.has_header if options.has_header is not None else sniff_header(head_rows)
    if has_header:
        source_names = ["" if v is None else str(v) for v in head_rows[0]]
        frame = raw.slice(1)
    else:
        source_names = [f"column_{i + 1}" for i in range(raw.width)]
        frame = raw
    frame = _positional(frame)
    frame = _drop_empty_trailing_columns(frame, source_names)
    _check_caps(frame)
    return ParsedTable(
        frame=frame,
        source_names=source_names[: frame.width],
        meta={"format": kind, "encoding": encoding, "delimiter": delimiter, "has_header": has_header},
    )


def _positional(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.rename({old: f"c{i}" for i, old in enumerate(frame.columns)})


def _drop_empty_trailing_columns(frame: pl.DataFrame, source_names: list[str]) -> pl.DataFrame:
    """Trailing columns with no header and no values (e.g. a trailing delimiter on every line) are dropped."""
    keep = frame.width
    while keep > 1:
        idx = keep - 1
        name = source_names[idx] if idx < len(source_names) else ""
        col = frame[f"c{idx}"]
        if name.strip() or col.drop_nulls().str.strip_chars().str.len_chars().sum() > 0:
            break
        keep -= 1
    if keep < frame.width:
        frame = frame.select([f"c{i}" for i in range(keep)])
        del source_names[keep:]
    return frame


def _check_caps(frame: pl.DataFrame) -> None:
    if frame.width > max_columns():
        raise IngestError("too_many_columns", f"The file has {frame.width} columns; the limit is {max_columns()}.")
    if frame.height > max_rows():
        raise IngestError("too_many_rows", f"The file has more than {max_rows()} rows.")


# ---------------------------------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------------------------------
def cell_to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0) and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value.total_seconds())
    text = str(value)
    return text if text != "" else None


def _forward_fill_merged(header: list[str | None], merged: Iterable, row_index: int) -> list[str | None]:
    out = list(header)
    for rng in merged or ():
        try:
            (r0, c0), (r1, c1) = rng
        except (TypeError, ValueError):
            continue
        if r0 <= row_index <= r1 and 0 <= c0 < len(out):
            top = out[c0]
            for c in range(c0 + 1, min(c1, len(out) - 1) + 1):
                if out[c] in (None, ""):
                    out[c] = top
    return out


def parse_excel(path: str, options: ParseOptions | None = None) -> ParsedTable:
    options = options or ParseOptions()
    try:
        import python_calamine

        wb = python_calamine.load_workbook(path)
    except Exception as exc:  # noqa: BLE001 - calamine raises several error types for corrupt files
        raise IngestError("malformed_file", "The Excel workbook could not be opened (corrupt or encrypted).") from exc
    try:
        names = list(wb.sheet_names)
        if options.sheet:
            if options.sheet not in names:
                raise IngestError("sheet_not_found", f"Sheet '{options.sheet}' was not found.", detail={"sheets": names})
            candidates = [options.sheet]
        else:
            candidates = names
        chosen = None
        rows: list[list[Any]] = []
        merged: list = []
        for name in candidates:
            try:
                sheet = wb.get_sheet_by_name(name)
                data = sheet.to_python(skip_empty_area=True)
            except Exception as exc:  # noqa: BLE001
                raise IngestError("malformed_file", f"Sheet '{name}' could not be read.") from exc
            data = [list(r) for r in data if any(cell_to_text(c) is not None for c in r)]
            if data:
                chosen, rows = name, data
                merged = list(getattr(sheet, "merged_cell_ranges", None) or [])
                break
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    if chosen is None:
        raise IngestError("upload_empty", "The workbook has no non-empty sheet.")
    width = max(len(r) for r in rows)
    text_rows = [[cell_to_text(c) for c in r] + [None] * (width - len(r)) for r in rows]
    header_rows = 1
    has_header = options.has_header if options.has_header is not None else sniff_header(text_rows)
    if has_header:
        header = _forward_fill_merged(text_rows[0], merged, 0)
        merged_in_header = any(
            isinstance(rng, (tuple, list)) and len(rng) == 2 and rng[0][0] == 0 and rng[1][1] > rng[0][1]
            for rng in merged
        )
        if merged_in_header and len(text_rows) > 2 and not any(_is_data_like(c) for c in text_rows[1]):
            sub = text_rows[1]
            header = [
                " ".join(p for p in (top, s) if p) or None for top, s in zip(header, sub, strict=False)
            ]
            header_rows = 2
        source_names = ["" if h is None else str(h) for h in header]
        body = text_rows[header_rows:]
    else:
        source_names = [f"column_{i + 1}" for i in range(width)]
        body = text_rows
    frame = pl.DataFrame(
        {f"c{i}": pl.Series(f"c{i}", [r[i] for r in body], dtype=pl.String) for i in range(width)}
    )
    frame = _drop_empty_trailing_columns(frame, source_names)
    _check_caps(frame)
    return ParsedTable(
        frame=frame,
        source_names=source_names[: frame.width],
        meta={"format": "excel", "sheet": chosen, "sheets": names, "has_header": has_header, "header_rows": header_rows},
    )


# ---------------------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------------------
def _scalar_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return repr(value)
    return str(value)


def flatten_record(record: dict, *, prefix: str = "", depth: int = 1, out: dict | None = None) -> dict:
    """{"a": {"b": 1}} -> {"a__b": 1}; dicts below depth 3 and all lists stay as Python objects (-> JSON)."""
    out = {} if out is None else out
    for key, value in record.items():
        name = f"{prefix}__{key}" if prefix else str(key)
        if isinstance(value, dict) and value and depth < JSON_MAX_DEPTH:
            flatten_record(value, prefix=name, depth=depth + 1, out=out)
        elif isinstance(value, dict) and not value:
            out.setdefault(name, None)
        else:
            out[name] = value
    return out


def _records_from_json_text(text: str, kind: str) -> list:
    stripped = text.lstrip()
    if kind == "json" or stripped.startswith("["):
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if stripped.startswith("{") and "Extra data" in str(exc):
                return _ndjson_records(stripped)
            raise IngestError(
                "malformed_file", f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."
            ) from exc
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict):
            lists = [v for v in doc.values() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)]
            if len(lists) == 1:
                return lists[0]
            return [doc]
        raise IngestError("malformed_file", "A JSON file must contain an array of objects.")
    return _ndjson_records(stripped)


def _ndjson_records(text: str) -> list:
    records = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise IngestError("malformed_file", f"Invalid JSON on line {lineno}: {exc.msg}.") from exc
    return records


def parse_json(path: str, kind: str = "json") -> ParsedTable:
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw.strip():
        raise IngestError("upload_empty", "The file is empty.")
    encoding, bom = detect_encoding(raw[:ENCODING_SAMPLE_BYTES])
    text = raw[bom:].decode(encoding, errors="replace")
    records = _records_from_json_text(text, kind)
    if not records:
        raise IngestError("upload_empty", "The JSON file contains no records.")
    columns: dict[str, list] = {}
    nested: set[str] = set()
    n = 0
    for rec in records:
        if not isinstance(rec, dict):
            rec = {"value": rec}
        flat = flatten_record(rec)
        for key, value in flat.items():
            col = columns.get(key)
            if col is None:
                if len(columns) >= max_columns():
                    raise IngestError("too_many_columns", f"The records have more than {max_columns()} fields.")
                col = columns[key] = [None] * n
            if isinstance(value, (dict, list)):
                nested.add(key)
            col.append(value)
        n += 1
        for key, col in columns.items():
            if len(col) < n:
                col.append(None)
        if n > max_rows():
            raise IngestError("too_many_rows", f"The file has more than {max_rows()} records.")
    names = list(columns)
    series = []
    json_positions: set[int] = set()
    for i, name in enumerate(names):
        values = columns[name]
        if name in nested:
            json_positions.add(i)
            texts = [None if v is None else json.dumps(v, ensure_ascii=False, default=str) for v in values]
        else:
            texts = [_scalar_text(v) for v in values]
        series.append(pl.Series(f"c{i}", texts, dtype=pl.String))
    frame = pl.DataFrame(series)
    _check_caps(frame)
    return ParsedTable(
        frame=frame,
        source_names=names,
        json_columns=json_positions,
        meta={"format": "json" if kind == "json" else "ndjson", "encoding": encoding, "records": n},
    )


# ---------------------------------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------------------------------
def parse_file(path: str, kind: str, options: ParseOptions | None = None) -> ParsedTable:
    if kind in ("csv", "tsv", "txt"):
        return parse_delimited(path, kind, options)
    if kind in ("xlsx", "xls"):
        return parse_excel(path, options)
    if kind in ("json", "ndjson"):
        return parse_json(path, kind)
    raise IngestError("upload_type_not_allowed", f"Unsupported file kind '{kind}'.")
