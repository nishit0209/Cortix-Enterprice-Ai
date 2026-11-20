"""Large-ingest performance (pure, no DB): parse + type inference + profiling of a generated 1,000,000-row CSV, then
the batched COPY serialisation the warehouse loader streams. Synthetic data only.

Run:  pytest apps/datasets/tests/test_perf_large_ingest.py -m slow -s
The timings are printed (and asserted against generous ceilings so a pathological regression fails).
"""

from __future__ import annotations

import time

import polars as pl
import pytest

from apps.datasets.ingest.analyze import analyze
from apps.datasets.ingest.parsing import parse_delimited
from apps.warehouse import services as warehouse

pytestmark = pytest.mark.slow

ROWS = 1_000_000
PARSE_PROFILE_CEILING_S = 240
COPY_SERIALISE_CEILING_S = 120


def _write_csv(path, rows: int) -> None:
    regions = dict(enumerate(["North", "South", "East", "West", "Central"]))
    df = pl.DataFrame({"idx": pl.int_range(0, rows, eager=True)}).with_columns(
        (pl.col("idx") + 100_000).alias("Order ID"),
        (pl.lit("2024-01-01").str.to_date() + pl.duration(days=pl.col("idx") % 365)).alias("Order Date"),
        (pl.col("idx") % 5).replace_strict(regions, return_dtype=pl.String).alias("Region"),
        ((pl.col("idx") * 37) % 10_000 / 100 + 1).round(2).alias("Revenue"),
        (pl.col("idx") % 20 + 1).alias("Quantity"),
        (pl.col("idx") % 3 == 0).alias("Returned"),
        pl.format("SKU-{}", pl.col("idx") % 5000).alias("Product Code"),
    ).drop("idx")
    df.write_csv(path)


def test_parse_and_profile_one_million_rows(tmp_path, capsys):
    path = tmp_path / "orders_1m.csv"
    t0 = time.perf_counter()
    _write_csv(path, ROWS)
    t_gen = time.perf_counter() - t0
    size_mb = path.stat().st_size / 1e6

    t1 = time.perf_counter()
    parsed = parse_delimited(str(path), "csv")
    t_parse = time.perf_counter() - t1
    assert parsed.row_count == ROWS

    t2 = time.perf_counter()
    analyzed = analyze(parsed)
    t_profile = time.perf_counter() - t2
    del parsed
    types = {c.name: c.data_type for c in analyzed.columns}
    assert analyzed.frame.height == ROWS
    assert types["order_id"] == "bigint" and types["revenue"] in ("double precision", "numeric")
    assert types["order_date"] == "date" and types["returned"] == "boolean"
    assert analyzed.profile["row_count"] == ROWS

    # the warehouse COPY payload: per-slice cast + CSV serialisation (what load_table streams to PostgreSQL)
    specs = [warehouse.ColumnSpec(c.name, c.data_type) for c in analyzed.columns]
    t3 = time.perf_counter()
    copied = 0
    batches = 0
    for chunk in analyzed.frame.iter_slices(n_rows=warehouse.COPY_BATCH_ROWS):
        copied += len(warehouse._frame_to_csv(warehouse.prepare_frame(chunk, specs)))
        batches += 1
    t_copy = time.perf_counter() - t3
    assert batches == ROWS // warehouse.COPY_BATCH_ROWS and copied > 0

    with capsys.disabled():
        print(
            f"\n[perf] 1M-row CSV ({size_mb:.1f} MB): generate {t_gen:.2f}s | parse {t_parse:.2f}s | "
            f"infer+profile+PII {t_profile:.2f}s | COPY serialise {batches}x{warehouse.COPY_BATCH_ROWS} rows "
            f"{t_copy:.2f}s ({copied / 1e6:.1f} MB)"
        )
    assert t_parse + t_profile < PARSE_PROFILE_CEILING_S
    assert t_copy < COPY_SERIALISE_CEILING_S
