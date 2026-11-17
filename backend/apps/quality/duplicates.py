"""Duplicate detection: exact duplicate rows, key duplicates, fuzzy value clusters (rapidfuzz + blocking).

Fuzzy clustering works on DISTINCT values of a text column: values are normalized (case, whitespace,
punctuation), blocked by a normalized prefix so comparisons stay near O(n), compared with rapidfuzz
token_sort_ratio (>= 90) or a length-aware Levenshtein typo rule inside each block, and merged with union-find.
Values of PII columns are never shown.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import polars as pl
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

FUZZY_THRESHOLD = 90
MAX_DISTINCT = 20_000
BLOCK_CAP = 1500
MAX_FUZZY_COLUMNS = 5
MAX_CLUSTERS = 20
MAX_VALUES_PER_CLUSTER = 10
MAX_TEXT_LEN = 80
FUZZY_SEMANTICS = {"category", "text", "geo_city", "geo_country", "person_name"}

_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).lower()
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _typo_match(a: str, b: str, distance: int) -> bool:
    """Short values: token ratios are too coarse, so allow 1 edit for 5-8 chars and 2 edits for 9+ chars."""
    shortest = min(len(a), len(b))
    if shortest < 5:
        return False
    return distance <= (1 if shortest < 9 else 2)


def _blocks(norms: Sequence[str], prefix: int = 2) -> dict[str, list[int]]:
    blocks: dict[str, list[int]] = defaultdict(list)
    for i, n in enumerate(norms):
        blocks[n[:prefix]].append(i)
    return blocks


def fuzzy_clusters(series: pl.Series, *, threshold: int = FUZZY_THRESHOLD, mask_values: bool = False) -> dict[str, Any]:
    """Cluster near-duplicate values. Returns {"clusters": [...], "inconsistent_rows": int, "checked_rows": int}."""
    nn = series.drop_nulls().cast(pl.String)
    if nn.len() == 0:
        return {"clusters": [], "inconsistent_rows": 0, "checked_rows": 0}
    vc = pl.DataFrame({"value": nn}).group_by("value").len().sort("len", descending=True).head(MAX_DISTINCT)
    values: list[str] = vc["value"].to_list()
    counts: list[int] = vc["len"].to_list()
    norms = [normalize_text(v) for v in values]
    uf = _UnionFind(len(values))
    # 1) identical after normalization ("Mumbai" / " mumbai" / "MUMBAI.")
    first_by_norm: dict[str, int] = {}
    for i, n in enumerate(norms):
        if n in first_by_norm:
            uf.union(first_by_norm[n], i)
        else:
            first_by_norm[n] = i
    # 2) fuzzy within prefix blocks, on distinct normalized forms
    reps = list(first_by_norm.items())  # (norm, index)
    rep_norms = [n for n, _ in reps]
    for members in _blocks(rep_norms).values():
        if len(members) < 2:
            continue
        if len(members) <= BLOCK_CAP:
            sub_blocks = [members]
        else:  # re-block large blocks on a longer prefix; cap what is left (most frequent values first)
            sub = _blocks([rep_norms[m] for m in members], 3)
            sub_blocks = [[members[i] for i in idxs] for idxs in sub.values()]
        for block in sub_blocks:
            block = block[:BLOCK_CAP]
            if len(block) < 2:
                continue
            texts = [rep_norms[m] for m in block]
            scores = process.cdist(texts, texts, scorer=fuzz.token_sort_ratio, score_cutoff=threshold, workers=1)
            edits = process.cdist(texts, texts, scorer=Levenshtein.distance, score_cutoff=2, workers=1)
            for a in range(len(block)):
                for b in range(a + 1, len(block)):
                    if scores[a][b] >= threshold or _typo_match(texts[a], texts[b], int(edits[a][b])):
                        uf.union(reps[block[a]][1], reps[block[b]][1])
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(values)):
        groups[uf.find(i)].append(i)
    clusters = []
    inconsistent = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: -counts[i])
        rows = sum(counts[i] for i in members)
        inconsistent += rows - counts[members[0]]
        clusters.append({
            "representative": "***" if mask_values else values[members[0]],
            "distinct_values": len(members),
            "rows": rows,
            "values": [
                {"value": "***" if mask_values else values[i], "count": counts[i]} for i in members[:MAX_VALUES_PER_CLUSTER]
            ],
        })
    clusters.sort(key=lambda c: -c["rows"])
    return {"clusters": clusters[:MAX_CLUSTERS], "inconsistent_rows": int(inconsistent), "checked_rows": int(nn.len())}


def key_duplicates(frame: pl.DataFrame, key_columns: Sequence[str], pii_columns: set[str]) -> list[dict]:
    out = []
    for col in key_columns:
        if col not in frame.columns:
            continue
        s = frame[col]
        nn = s.drop_nulls()
        if nn.len() == 0:
            continue
        dup_rows = int(nn.len() - nn.n_unique())
        sample: list[Any] = []
        if dup_rows:
            vc = pl.DataFrame({"v": nn}).group_by("v").len().filter(pl.col("len") > 1).sort("len", descending=True)
            dup_values = vc.height
            for v, c in zip(vc["v"].head(5).to_list(), vc["len"].head(5).to_list(), strict=True):
                shown = "***" if col in pii_columns else (v.isoformat() if hasattr(v, "isoformat") else v)
                sample.append({"value": shown, "count": int(c)})
        else:
            dup_values = 0
        out.append({"column": col, "non_null": int(nn.len()), "duplicate_rows": dup_rows,
                    "duplicate_values": int(dup_values), "sample": sample})
    return out


def find_duplicates(
    frame: pl.DataFrame,
    columns: Sequence[Mapping[str, Any]],
    *,
    key_columns: Sequence[str],
) -> dict[str, Any]:
    """columns: [{name, data_type, semantic_type, is_pii, stats}] of the version."""
    pii = {c["name"] for c in columns if c.get("is_pii")}
    exact = int(frame.height - frame.n_unique()) if frame.height and frame.width else 0
    keys = key_duplicates(frame, key_columns, pii)
    fuzzy_out: list[dict] = []
    inconsistent = 0
    checked = 0
    candidates = []
    for c in columns:
        if c["data_type"] != "text" or c.get("is_pii") or c.get("semantic_type") not in FUZZY_SEMANTICS:
            continue
        stats = c.get("stats") or {}
        distinct = int(stats.get("distinct") or 0)
        if distinct < 2 or (stats.get("max_length") or 0) > MAX_TEXT_LEN:
            continue
        candidates.append(c["name"])
    for name in candidates[:MAX_FUZZY_COLUMNS]:
        result = fuzzy_clusters(frame[name])
        inconsistent += result["inconsistent_rows"]
        checked += result["checked_rows"]
        for cluster in result["clusters"]:
            fuzzy_out.append({"column": name, **cluster})
    fuzzy_out.sort(key=lambda c: -c["rows"])
    return {
        "exact_rows": exact,
        "key_duplicates": sum(k["duplicate_rows"] for k in keys),
        "key_columns": keys,
        "fuzzy_clusters": fuzzy_out[:MAX_CLUSTERS],
        "inconsistent_rows": inconsistent,
        "consistency_checked_rows": checked,
    }
