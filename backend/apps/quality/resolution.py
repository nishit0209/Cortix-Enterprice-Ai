"""Entity-resolution primitives shared by Customer 360 and Product 360 (pure Python, no DB).

    UnionFind            union-find with cannot-link constraints (rejected pairs are never merged, even transitively)
    candidate_pairs      blocking: items sharing any block key become candidate pairs (oversized blocks are skipped)
    reconcile_clusters   keeps entity ids stable across runs (a cluster keeps the id that owns most of its records)
    pair_key             canonical (min, max) order of a pair
    soundex              phonetic key for name blocking
    proximity            1.0 for equal numbers, decaying linearly to 0.0 at `tolerance` relative difference

Re-exported by apps.quality.services for the other apps.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_MAX_BLOCK = 1000


def pair_key(a, b) -> tuple:
    """(a, b) in a canonical order (by string form, so UUIDs and ints sort consistently)."""
    return (a, b) if str(a) <= str(b) else (b, a)


class UnionFind:
    """Disjoint sets over hashable items with cannot-link constraints.

    `forbid(a, b)` records that a and b must never end up in the same set; `union(a, b)` refuses (returns False)
    when merging the two sets would put any forbidden pair together. Unions are applied in call order, so callers
    feed the strongest evidence first (confirmed pairs, then pairs by descending score)."""

    def __init__(self, items: Iterable[Hashable] = ()):
        self.parent: dict = {}
        self.members: dict = {}
        self.blocked: dict = defaultdict(set)  # root -> set of items its set may not contain
        for item in items:
            self.add(item)

    def add(self, item) -> None:
        if item not in self.parent:
            self.parent[item] = item
            self.members[item] = {item}

    def find(self, item):
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def forbid(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        self.blocked[ra].add(b)
        self.blocked[rb].add(a)

    def can_union(self, a, b) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        return not (self.blocked[ra] & self.members[rb]) and not (self.blocked[rb] & self.members[ra])

    def union(self, a, b) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if not self.can_union(a, b):
            return False
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra] |= self.members.pop(rb)
        self.blocked[ra] |= self.blocked.pop(rb, set())
        return True

    def same(self, a, b) -> bool:
        return self.find(a) == self.find(b)

    def groups(self) -> list[list]:
        """Every set as a list (sorted by string form), largest sets first, then by first member."""
        out = [sorted(m, key=str) for m in self.members.values()]
        return sorted(out, key=lambda g: (-len(g), str(g[0])))


@dataclass
class BlockingStats:
    blocks: int = 0
    pairs: int = 0
    skipped_blocks: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"blocks": self.blocks, "pairs": self.pairs, "skipped_blocks": self.skipped_blocks[:20],
                "skipped_block_count": len(self.skipped_blocks)}


def candidate_pairs(keys_by_item: Mapping[Hashable, Iterable[str]], *,
                    max_block: int = DEFAULT_MAX_BLOCK) -> tuple[set[tuple], BlockingStats]:
    """Items that share at least one block key become candidate pairs (each pair once, in pair_key order).

    A block larger than `max_block` would cost O(n^2) comparisons; it is skipped and reported (its items are still
    compared through their other, more selective keys)."""
    blocks: dict[str, list] = defaultdict(list)
    for item, keys in keys_by_item.items():
        for key in set(k for k in keys if k):
            blocks[key].append(item)
    stats = BlockingStats()
    pairs: set[tuple] = set()
    for key, items in blocks.items():
        if len(items) < 2:
            continue
        if len(items) > max_block:
            stats.skipped_blocks.append({"key": key.split(":", 1)[0], "size": len(items)})
            continue
        stats.blocks += 1
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[i] != items[j]:
                    pairs.add(pair_key(items[i], items[j]))
    stats.pairs = len(pairs)
    return pairs, stats


def reconcile_clusters(clusters: Sequence[Sequence[Hashable]],
                       previous: Mapping[Hashable, Hashable]) -> list[tuple[Hashable | None, list]]:
    """Map new clusters onto existing entity ids so ids stay stable across runs.

    `previous` maps record -> entity id from the last run. Each existing entity id goes to the cluster holding most
    of its records (ties: the larger cluster, then the first); clusters without one get None (a new entity).
    Returns [(entity_id | None, cluster)] in the order of `clusters`."""
    overlap: dict[Hashable, list[tuple[int, int, int]]] = defaultdict(list)
    for idx, cluster in enumerate(clusters):
        counts: dict[Hashable, int] = defaultdict(int)
        for rec in cluster:
            ent = previous.get(rec)
            if ent is not None:
                counts[ent] += 1
        for ent, n in counts.items():
            overlap[ent].append((n, len(cluster), -idx))
    assigned: dict[int, Hashable] = {}
    # strongest claims first: most records, then larger cluster, then earlier cluster; entity id breaks ties
    claims = sorted(((n, size, neg_idx, str(ent), ent) for ent, items in overlap.items()
                     for n, size, neg_idx in items), reverse=True)
    used: set = set()
    for _n, _size, neg_idx, _s, ent in claims:
        idx = -neg_idx
        if ent in used or idx in assigned:
            continue
        assigned[idx] = ent
        used.add(ent)
    return [(assigned.get(i), list(c)) for i, c in enumerate(clusters)]


# ----------------------------------------------------------------------------------------------- text helpers
_SOUNDEX_CODES = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"),
                  "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}


def fold(text: str) -> str:
    """Unicode-normalized, accent-free, lower-case text (NFKD, combining marks removed)."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def soundex(word: str) -> str:
    """American Soundex (letter + 3 digits) of the first alphabetic run of `word`; "" when there is none."""
    letters = re.sub(r"[^a-z]", "", fold(word))
    if not letters:
        return ""
    first, out, last = letters[0], [], _SOUNDEX_CODES.get(letters[0], "")
    for ch in letters[1:]:
        code = _SOUNDEX_CODES.get(ch, "")
        if code and code != last:
            out.append(code)
        if ch not in "hw":
            last = code
    return (first.upper() + "".join(out) + "000")[:4]


def proximity(a: float | None, b: float | None, *, tolerance: float = 0.5) -> float | None:
    """Similarity of two positive numbers: 1.0 when equal, linearly down to 0.0 at `tolerance` relative difference."""
    if a is None or b is None:
        return None
    a, b = float(a), float(b)
    if a == b:
        return 1.0
    scale = max(abs(a), abs(b))
    if scale == 0:
        return 1.0
    rel = abs(a - b) / scale
    return max(0.0, 1.0 - rel / tolerance)
