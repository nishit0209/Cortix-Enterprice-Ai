"""Product 360 normalization, matching, quality and rule enrichment: pure functions (no DB).
`apps.products.services` runs them over the org's source records.

Normalization
    sku       upper-cased, separators (space - _ / . :) removed, a known legacy prefix (OLD, LEGACY, OBS, DISC)
              dropped. `sku_stem` additionally drops a trailing variant suffix: a pack label (5KG, 500ML, 2X100G,
              12PC), a size code (XS S M L XL XXL ...) or a colour code (RED, BLU, BLK ...).
    brand     folded (accents, case, punctuation) and mapped through the org's brand aliases; an unmapped brand is
              taken from a parenthesised "(Brand)" in the name, else the first known brand among the name tokens
              (known = every mapped/parenthesised brand of the run), else the first name word.
    quantity  "500ml", "0.5 L", "1,5 kg", "2 x 100 g", "12 pcs", "1 pair" -> (value, unit) in a BASE unit
              (g, ml, pc, pair, m): 0.5 L == 500 ml. A mapped pack_size + unit column wins over the name.
    name      folded words with the brand and pack-size tokens removed ("core name"): the comparable product name.
    attributes regex rules on the name: size (S..XXL, numeric sizes), colour, pack count ("pack of 6", "6 pack",
              "2 x 100 g"), material (cotton, silk, steel ...), capacity (10000mAh, 5L on a non-liquid), power (9W).

Matching (duplicates of the SAME product; pack-size variants are DIFFERENT products)
    blocking  brand + category (folded), the SKU stem, and brand + first core-name word.
    features  name    token-set / token-sort ratio of the core names (typos tolerated)
              sku     1.0 same normalized SKU, 0.9 same SKU stem, else 0.0 (None when a side has none)
              size    1.0 same quantity in the base unit, else 0.0 (None when a side has none)
              price   1.0 equal, linearly to 0.0 at a 50 % difference
              brand   1.0 same canonical brand, else 0.0
    score     weighted mean of the available features (DEFAULT_WEIGHTS), then rules: different pack size -> at most
              0.45 (a variant, never merged); same normalized SKU and same size -> at least 0.97; different brands
              -> at most 0.5; core names below 0.6 similarity -> at most 0.6.
    decide    score >= AUTO (default 0.90) merges, REVIEW (default 0.75) <= score < AUTO -> review queue.

Quality score (0-100 per master): mean of completeness (sku, name, brand, category, price, size present),
validity (price > 0 and < 10^9, a known unit, SKU of 3-40 [A-Z0-9], name of >= 3 characters and not all upper-case,
category not blank) and consistency (linked records agree on brand / category / size, price spread <= 20 %).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from apps.quality.services import fold, proximity

DEFAULT_WEIGHTS = {"name": 0.40, "sku": 0.20, "size": 0.15, "price": 0.15, "brand": 0.10}
DEFAULT_AUTO = 0.90
DEFAULT_REVIEW = 0.75
MAPPING_FIELDS = ("sku", "name", "brand", "category", "price", "pack_size", "unit", "attributes", "external_id")

LEGACY_PREFIXES = ("OLD", "LEGACY", "OBS", "DISC")
SIZE_CODES = ("XXXL", "XXL", "XL", "XS", "XXS", "S", "M", "L")
COLOUR_CODES = {"RED", "BLU", "BLK", "WHT", "GRN", "YEL", "GRY", "PNK", "BRN", "ORG", "PUR", "NVY", "BLUE", "BLACK",
                "WHITE", "GREEN"}
COLOURS = ("black", "white", "red", "blue", "green", "yellow", "grey", "gray", "pink", "brown", "orange", "purple",
           "navy", "maroon", "beige", "gold", "silver")
MATERIALS = ("cotton", "silk", "steel", "stainless steel", "leather", "denim", "wool", "linen", "polyester", "plastic",
             "glass", "wood", "wooden", "bamboo", "aluminium", "aluminum", "ceramic", "copper", "brass", "rayon")
# unit aliases -> (base unit, factor to base)
UNITS: dict[str, tuple[str, float]] = {
    "g": ("g", 1), "gm": ("g", 1), "gms": ("g", 1), "gram": ("g", 1), "grams": ("g", 1), "gr": ("g", 1),
    "kg": ("g", 1000), "kgs": ("g", 1000), "kilo": ("g", 1000), "kilogram": ("g", 1000), "mg": ("g", 0.001),
    "ml": ("ml", 1), "millilitre": ("ml", 1), "milliliter": ("ml", 1),
    "l": ("ml", 1000), "ltr": ("ml", 1000), "litre": ("ml", 1000), "liter": ("ml", 1000), "lt": ("ml", 1000),
    "cl": ("ml", 10),
    "pc": ("pc", 1), "pcs": ("pc", 1), "piece": ("pc", 1), "pieces": ("pc", 1), "unit": ("pc", 1),
    "units": ("pc", 1), "nos": ("pc", 1), "no": ("pc", 1), "ea": ("pc", 1), "each": ("pc", 1),
    "pair": ("pair", 1), "pairs": ("pair", 1), "pr": ("pair", 1),
    "m": ("m", 1), "mtr": ("m", 1), "cm": ("m", 0.01), "mm": ("m", 0.001),
}
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
_NUM = r"\d+(?:[.,]\d+)?"
_QTY_RE = re.compile(rf"(?<![\w.])(?:(\d+)\s*[x×*]\s*)?({_NUM})\s*({_UNIT_ALT})\b", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^()]{1,60})\)")
_SKU_SEP_RE = re.compile(r"[\s\-_/.:#]+")
_PACK_LABEL_RE = re.compile(rf"(?:\d+X)?\d+(?:\.\d+)?(?:{_UNIT_ALT})$", re.IGNORECASE)
_PACK_COUNT_RE = re.compile(r"\b(?:pack\s+of\s+(\d+)|(\d+)\s*[- ]?(?:pack|pk|pcs? pack))\b", re.IGNORECASE)
_SIZE_RE = re.compile(r"\bsize\s*[:\-]?\s*(\w{1,4})\b|\b(XXS|XS|S|M|L|XL|XXL|XXXL)\b", re.IGNORECASE)
_CAPACITY_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mah|wh)\b", re.IGNORECASE)
_POWER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(w|kw|watt|watts)\b", re.IGNORECASE)
_SPF_RE = re.compile(r"\bspf\s*(\d{1,3})\b", re.IGNORECASE)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value).strip()


# ----------------------------------------------------------------------------------------------- SKU
def normalize_sku(value: Any) -> str | None:
    raw = _text(value).upper()
    if not raw:
        return None
    parts = [p for p in _SKU_SEP_RE.split(raw) if p]
    if len(parts) > 1 and parts[0] in LEGACY_PREFIXES:
        parts = parts[1:]
    sku = "".join(parts)
    for prefix in LEGACY_PREFIXES:  # compact legacy prefix ("OLDNILBASRIC5KG") only when separated originally
        if sku.startswith(prefix) and len(parts) == 1 and raw.startswith(prefix + "-"):
            sku = sku[len(prefix):]
    return sku or None


def sku_stem(value: Any) -> str | None:
    """Normalized SKU without a trailing variant suffix (pack label, size code or colour code)."""
    raw = _text(value).upper()
    if not raw:
        return None
    parts = [p for p in _SKU_SEP_RE.split(raw) if p]
    if len(parts) > 1 and parts[0] in LEGACY_PREFIXES:
        parts = parts[1:]
    if len(parts) > 1 and (_PACK_LABEL_RE.fullmatch(parts[-1]) or parts[-1] in SIZE_CODES
                           or parts[-1] in COLOUR_CODES):
        parts = parts[:-1]
    stem = "".join(parts)
    if len(parts) == 1:  # compact SKU: strip a trailing pack label ("NILBASRIC5KG" -> "NILBASRIC")
        m = re.search(rf"(\d+(?:\.\d+)?)({_UNIT_ALT})$", stem, re.IGNORECASE)
        if m and m.start() >= 3 and not stem[m.start() - 1].isdigit():
            stem = stem[:m.start()]
    return stem or None


# ----------------------------------------------------------------------------------------------- quantity
def parse_number(value: Any) -> float | None:
    text = _text(value).replace(",", ".") if not isinstance(value, int | float) else value
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) < 1e12 else None


def to_base(value: float | None, unit: Any) -> tuple[float, str] | None:
    """(value, unit) -> (value in base unit, base unit); None for unknown units."""
    if value is None:
        return None
    u = fold(_text(unit)).replace(".", "").strip()
    if not u:
        return None
    if u not in UNITS:
        return None
    base, factor = UNITS[u]
    return round(value * factor, 6), base


def parse_quantity(text: Any) -> tuple[float, str] | None:
    """The LAST pack quantity in a text ("Rice 5kg", "OIL 0.5 L", "2 x 100 g") in its base unit."""
    found = list(_QTY_RE.finditer(_text(text)))
    if not found:
        return None
    m = found[-1]
    count = int(m.group(1)) if m.group(1) else 1
    value = parse_number(m.group(2))
    out = to_base(value * count if value is not None else None, m.group(3))
    return out


def format_quantity(q: tuple[float, str] | None) -> str | None:
    if not q:
        return None
    value, unit = q
    if unit == "g" and value >= 1000:
        return f"{value / 1000:g} kg"
    if unit == "ml" and value >= 1000:
        return f"{value / 1000:g} l"
    return f"{value:g} {unit}"


# ----------------------------------------------------------------------------------------------- brand / name
def brand_key(value: Any) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", fold(_text(value))).split())


def canonical_brand(value: Any, aliases: Mapping[str, str] | None = None) -> str | None:
    """Folded brand key mapped through the org's aliases ({alias: canonical display name})."""
    key = brand_key(value)
    if not key:
        return None
    alias_map = {brand_key(k): v for k, v in (aliases or {}).items()}
    return brand_key(alias_map.get(key, key)) or None


def parenthesised_brand(name: Any) -> str | None:
    found = _PAREN_RE.findall(_text(name))
    for inner in reversed(found):
        if not _QTY_RE.search(inner) and re.search(r"[A-Za-z]", inner):
            return inner.strip()
    return None


def infer_brand(name: Any, known: Iterable[str]) -> str | None:
    paren = parenthesised_brand(name)
    if paren:
        return paren
    tokens = brand_key(name).split()
    known_keys = sorted({brand_key(k) for k in known if brand_key(k)}, key=len, reverse=True)
    joined = " " + " ".join(tokens) + " "
    for k in known_keys:
        if f" {k} " in joined:
            return k
    return tokens[0] if tokens else None


def core_name(name: Any, brand: str | None) -> str:
    """Folded product name without the brand, parenthesised brand and pack-size tokens."""
    text = _PAREN_RE.sub(lambda m: " " if brand and brand_key(m.group(1)) == brand else f" {m.group(1)} ",
                         _text(name))
    found = list(_QTY_RE.finditer(text))
    if found:  # drop the pack quantity (the last one); other quantities (5L cooker, 10000mAh) stay in the name
        last = found[-1]
        text = text[:last.start()] + " " + text[last.end():]
    words = brand_key(text).split()
    if brand:
        bw = brand.split()
        for i in range(len(words) - len(bw) + 1):
            if words[i:i + len(bw)] == bw:
                words = words[:i] + words[i + len(bw):]
                break
    return " ".join(words)


def display_case(name: str) -> str:
    """Title-case an all-upper-case name while keeping alphanumeric codes (SPF50, USB-C, 10000mAh) intact."""
    def fix(word: str) -> str:
        if any(ch.isdigit() for ch in word) or len(word) <= 2 and word.isalpha() and word.isupper() and word in (
                "TV", "AC", "LED", "USB"):
            return word if not word.isalpha() else word
        return word[:1].upper() + word[1:].lower()

    return " ".join(fix(w) for w in name.split())


# ----------------------------------------------------------------------------------------------- attributes
def extract_attributes(name: Any) -> dict[str, str]:
    """Regex attribute rules on a product name -> {key: value}."""
    text = _text(name)
    low = fold(text)
    out: dict[str, str] = {}
    m = _PACK_COUNT_RE.search(text)
    if m:
        out["pack_count"] = m.group(1) or m.group(2)
    else:
        q = re.search(r"\b(\d+)\s*[x×*]\s*\d", text)
        if q and int(q.group(1)) > 1:
            out["pack_count"] = q.group(1)
    sm = _SIZE_RE.search(text)
    if sm and (sm.group(1) or (sm.group(2) and sm.group(2).isupper())):
        out["size"] = (sm.group(1) or sm.group(2)).upper()
    for colour in COLOURS:
        if re.search(rf"\b{colour}\b", low):
            out["colour"] = "grey" if colour == "gray" else colour
            break
    for material in sorted(MATERIALS, key=len, reverse=True):
        if re.search(rf"\b{material}\b", low):
            out["material"] = {"aluminum": "aluminium", "wooden": "wood"}.get(material, material)
            break
    cm = _CAPACITY_RE.search(text)
    if cm:
        out["capacity"] = f"{parse_number(cm.group(1)):g} {cm.group(2).lower()}"
    pm = _POWER_RE.search(text)
    if pm:
        unit = pm.group(2).lower()
        watts = parse_number(pm.group(1)) * (1000 if unit == "kw" else 1)
        out["power"] = f"{watts:g} W"
    spf = _SPF_RE.search(text)
    if spf:
        out["spf"] = spf.group(1)
    qty = list(_QTY_RE.finditer(text))
    if len(qty) > 1:  # a quantity that is not the pack size: "Pressure Cooker 5L" -> capacity 5 l
        first = qty[0]
        base = to_base(parse_number(first.group(2)), first.group(3))
        if base and "capacity" not in out:
            out["capacity"] = format_quantity(base)
    return out


# ----------------------------------------------------------------------------------------------- records
def normalize_record(raw: Mapping[str, Any], *, aliases: Mapping[str, str] | None = None,
                     known_brands: Iterable[str] = ()) -> dict:
    """Mapped source values -> normalized fields (sku, brand, core name, base quantity, price, attributes)."""
    name = " ".join(_text(raw.get("name")).split())
    brand_raw = _text(raw.get("brand")) or infer_brand(name, known_brands)
    brand = canonical_brand(brand_raw, aliases)
    quantity = None
    if raw.get("pack_size") not in (None, "") and raw.get("unit") not in (None, ""):
        quantity = to_base(parse_number(raw.get("pack_size")), raw.get("unit"))
    if quantity is None and raw.get("unit") not in (None, "") and raw.get("pack_size") in (None, ""):
        quantity = parse_quantity(f"1 {raw.get('unit')}") if fold(_text(raw.get("unit"))) in ("pc", "pcs",
                                                                                          "pair") else None
    if quantity is None:
        quantity = parse_quantity(name)
    price = parse_number(raw.get("price"))
    extracted = extract_attributes(name)
    supplied = raw.get("attributes") if isinstance(raw.get("attributes"), Mapping) else {}
    category = " ".join(_text(raw.get("category")).split())
    return {
        "sku": normalize_sku(raw.get("sku")), "sku_raw": _text(raw.get("sku")) or None,
        "sku_stem": sku_stem(raw.get("sku")),
        "name": name or None, "core_name": core_name(name, brand) or None,
        "brand": brand, "brand_display": _text(raw.get("brand")) or (brand_raw or None),
        "category": category or None, "category_key": brand_key(category) or None,
        "quantity": list(quantity) if quantity else None,
        "price": round(price, 2) if price is not None and price >= 0 else None,
        "attributes": {**{k: _text(v) for k, v in supplied.items() if _text(v)}},
        "extracted": extracted,
    }


def known_brands_of(raws: Iterable[Mapping[str, Any]]) -> set[str]:
    out = set()
    for raw in raws:
        if _text(raw.get("brand")):
            out.add(_text(raw.get("brand")))
        paren = parenthesised_brand(raw.get("name"))
        if paren:
            out.add(paren)
    return out


# ----------------------------------------------------------------------------------------------- blocking / scoring
def blocking_keys(norm: Mapping[str, Any]) -> list[str]:
    keys = []
    brand, cat = norm.get("brand") or "", norm.get("category_key") or ""
    if brand and cat:
        keys.append(f"bc:{brand}:{cat}")
    if norm.get("sku_stem"):
        keys.append(f"ss:{norm['sku_stem']}")
    words = (norm.get("core_name") or "").split()
    if brand and words:
        keys.append(f"bn:{brand}:{words[0][:4]}")
    return keys


def name_similarity(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    if a == b:
        return 1.0
    return round(max(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0, 4)


def _sku_sim(a: Mapping, b: Mapping) -> float | None:
    if not a.get("sku") or not b.get("sku"):
        return None
    if a["sku"] == b["sku"]:
        return 1.0
    if a.get("sku_stem") and a.get("sku_stem") == b.get("sku_stem"):
        return 0.9
    return 0.0


def _size_sim(a: Mapping, b: Mapping) -> float | None:
    qa, qb = a.get("quantity"), b.get("quantity")
    if not qa or not qb:
        return None
    if qa[1] != qb[1]:
        return 0.0
    return 1.0 if abs(float(qa[0]) - float(qb[0])) <= 1e-6 * max(1.0, abs(float(qa[0]))) else 0.0


@dataclass(frozen=True)
class PairScore:
    score: float
    features: dict = field(default_factory=dict)
    rules: tuple[str, ...] = ()


def score_pair(a: Mapping[str, Any], b: Mapping[str, Any], weights: Mapping[str, float] | None = None) -> PairScore:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    feats = {
        "name": name_similarity(a.get("core_name"), b.get("core_name")),
        "sku": _sku_sim(a, b), "size": _size_sim(a, b),
        "price": proximity(a.get("price"), b.get("price")) if a.get("price") and b.get("price") else None,
        "brand": (1.0 if a.get("brand") == b.get("brand") else 0.0) if a.get("brand") and b.get("brand") else None,
    }
    if feats["price"] is not None:
        feats["price"] = round(feats["price"], 4)
    present = {k: v for k, v in feats.items() if v is not None and weights.get(k, 0) > 0}
    covered = sum(float(weights[k]) for k in present)
    if not present or covered <= 0:
        return PairScore(0.0, feats, ())
    score = sum(float(weights[k]) * v for k, v in present.items()) / covered
    rules: list[str] = []
    if feats["sku"] == 1.0 and feats["size"] != 0.0:
        score, rules = max(score, 0.97), [*rules, "same_sku"]
    if feats["size"] == 0.0:
        score, rules = min(score, 0.45), [*rules, "different_pack_size"]
    if feats["brand"] == 0.0:
        score, rules = min(score, 0.5), [*rules, "different_brand"]
    if feats["name"] is not None and feats["name"] < 0.6:
        score, rules = min(score, 0.6), [*rules, "different_name"]
    return PairScore(round(min(max(score, 0.0), 1.0), 4), feats, tuple(rules))


def decide(score: float, *, auto: float = DEFAULT_AUTO, review: float = DEFAULT_REVIEW) -> str | None:
    if score >= auto:
        return "auto_merged"
    if score >= review:
        return "pending_review"
    return None


def validate_thresholds(auto: float, review: float) -> None:
    if not (0 < review < auto <= 1):
        raise ValueError("Thresholds must satisfy 0 < review < auto <= 1.")


@dataclass
class Resolution:
    matches: dict[tuple, tuple[PairScore, str]]
    clusters: list[list]
    stats: dict


def resolve(records: Mapping[Any, Mapping[str, Any]], *, weights: Mapping[str, float] | None = None,
            auto: float = DEFAULT_AUTO, review: float = DEFAULT_REVIEW, confirmed: Iterable[tuple] = (),
            rejected: Iterable[tuple] = (), max_block: int = 1000) -> Resolution:
    """Block, score and cluster (union-find; confirmed first, rejected never, auto by descending score)."""
    from apps.quality.services import UnionFind, candidate_pairs, pair_key

    validate_thresholds(auto, review)
    pairs, block_stats = candidate_pairs({rid: blocking_keys(n) for rid, n in records.items()}, max_block=max_block)
    confirmed = {pair_key(*p) for p in confirmed}
    rejected = {pair_key(*p) for p in rejected}
    uf = UnionFind(records.keys())
    for a, b in rejected:
        if a in records and b in records:
            uf.forbid(a, b)
    for a, b in sorted(confirmed, key=str):
        if a in records and b in records:
            uf.union(a, b)
    matches: dict[tuple, tuple[PairScore, str]] = {}
    for pair in pairs:
        if pair in rejected or pair in confirmed:
            continue
        result = score_pair(records[pair[0]], records[pair[1]], weights)
        status = decide(result.score, auto=auto, review=review)
        if status:
            matches[pair] = (result, status)
    blocked = 0
    for pair, (_result, status) in sorted(matches.items(), key=lambda kv: (-kv[1][0].score, str(kv[0]))):
        if status == "auto_merged" and not uf.union(*pair):
            blocked += 1
    clusters = uf.groups()
    stats = {
        "records": len(records), "candidate_pairs": len(pairs),
        **{f"blocking_{k}": v for k, v in block_stats.as_dict().items() if k != "pairs"},
        "auto_merged": sum(1 for _, s in matches.values() if s == "auto_merged"),
        "pending_review": sum(1 for _, s in matches.values() if s == "pending_review"),
        "confirmed": len(confirmed), "rejected": len(rejected), "merges_blocked_by_rejections": blocked,
        "masters": len(clusters), "multi_record_masters": sum(1 for c in clusters if len(c) > 1),
    }
    return Resolution(matches, clusters, stats)


# ----------------------------------------------------------------------------------------------- golden / quality
@dataclass(frozen=True)
class Member:
    record_id: Any
    source_system: str
    priority: int
    normalized: Mapping[str, Any]


def _most_common(values: Sequence, members: Sequence[Member], key) -> Any:
    have = [(v, m) for v, m in zip(values, members, strict=True) if v not in (None, "")]
    if not have:
        return None
    counts = Counter(key(v) for v, _m in have)
    best = max(counts.values())
    tied = [(v, m) for v, m in have if counts[key(v)] == best]
    return sorted(tied, key=lambda vm: (vm[1].priority, str(vm[1].record_id)))[0][0]


def master_values(members: Sequence[Member]) -> dict:
    """Golden product values of one cluster: the most trusted source's SKU and name, the most frequent brand /
    category / size, the median price, merged attributes (supplied over extracted)."""
    ordered = sorted(members, key=lambda m: (m.priority, str(m.record_id)))
    norms = [m.normalized for m in ordered]
    sku = next((n.get("sku") for n in norms if n.get("sku")), None)
    names = [n.get("name") for n in norms]
    name = next((nm for nm in names if nm and not nm.isupper()), None) or next((nm for nm in names if nm), None)
    if name and name.isupper():
        name = display_case(name)
    brand_key_ = _most_common([n.get("brand") for n in norms], ordered, str)
    brand_display = next((n.get("brand_display") for n in norms if n.get("brand") == brand_key_
                          and n.get("brand_display") and not str(n.get("brand_display")).isupper()), None)
    brand_display = brand_display or (brand_key_.title() if brand_key_ else None)
    category = _most_common([n.get("category") for n in norms], ordered, brand_key)
    if category and category.islower():
        category = category.title()
    quantity = _most_common([tuple(n["quantity"]) if n.get("quantity") else None for n in norms], ordered, str)
    prices = [float(n["price"]) for n in norms if n.get("price") is not None]
    attributes: dict[str, str] = {}
    for n in reversed(norms):  # most trusted last so it wins
        attributes.update(n.get("extracted") or {})
    for n in reversed(norms):
        attributes.update(n.get("attributes") or {})
    return {
        "sku": sku, "name": name, "brand": brand_display, "brand_key": brand_key_, "category": category,
        "quantity": list(quantity) if quantity else None, "size": format_quantity(quantity),
        "price": round(statistics.median(prices), 2) if prices else None,
        "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
        "attributes": attributes, "core_name": next((n.get("core_name") for n in norms if n.get("core_name")), None),
    }


KNOWN_BASE_UNITS = {"g", "ml", "pc", "pair", "m"}


def quality_score(values: Mapping[str, Any], members: Sequence[Member] = ()) -> dict:
    """{completeness, validity, consistency, overall (0-100), issues[]} of one master."""
    issues: list[str] = []
    required = ("sku", "name", "brand", "category", "price", "size")
    present = [k for k in required if values.get(k) not in (None, "")]
    for k in required:
        if k not in present:
            issues.append(f"missing_{k}")
    completeness = len(present) / len(required)
    checks = []
    price = values.get("price")
    if price is not None:
        ok = 0 < float(price) < 1e9
        checks.append(ok)
        if not ok:
            issues.append("invalid_price")
    q = values.get("quantity")
    if q:
        ok = q[1] in KNOWN_BASE_UNITS and float(q[0]) > 0
        checks.append(ok)
        if not ok:
            issues.append("invalid_size")
    sku = values.get("sku")
    if sku:
        ok = bool(re.fullmatch(r"[A-Z0-9]{3,40}", sku))
        checks.append(ok)
        if not ok:
            issues.append("invalid_sku")
    name = values.get("name") or ""
    if name:
        ok = len(name) >= 3 and not name.isupper()
        checks.append(ok)
        if not ok:
            issues.append("name_format")
    validity = (sum(checks) / len(checks)) if checks else 0.0
    cons_checks = []
    norms = [m.normalized for m in members]
    if len(norms) > 1:
        for key, issue in (("brand", "inconsistent_brand"), ("category_key", "inconsistent_category")):
            vals = {n.get(key) for n in norms if n.get(key)}
            cons_checks.append(len(vals) <= 1)
            if len(vals) > 1:
                issues.append(issue)
        sizes = {tuple(n["quantity"]) for n in norms if n.get("quantity")}
        cons_checks.append(len(sizes) <= 1)
        if len(sizes) > 1:
            issues.append("inconsistent_size")
        prices = [float(n["price"]) for n in norms if n.get("price")]
        if len(prices) > 1:
            spread_ok = (max(prices) - min(prices)) / max(prices) <= 0.2
            cons_checks.append(spread_ok)
            if not spread_ok:
                issues.append("price_spread")
    consistency = (sum(cons_checks) / len(cons_checks)) if cons_checks else 1.0
    overall = round(100 * (completeness + validity + consistency) / 3, 2)
    return {"completeness": round(completeness * 100, 2), "validity": round(validity * 100, 2),
            "consistency": round(consistency * 100, 2), "overall": overall, "issues": issues}


# ----------------------------------------------------------------------------------------------- rule enrichment
@dataclass(frozen=True)
class Suggestion:
    field: str
    value: Any
    reason: str
    confidence: float


def rule_suggestions(values: Mapping[str, Any], *, category_by_core: Mapping[str, str] | None = None,
                     category_spelling: Mapping[str, str] | None = None) -> list[Suggestion]:
    """Local enrichment rules for one master -> proposed corrections (never applied without approval)."""
    out: list[Suggestion] = []
    name = values.get("name") or ""
    if name and name.isupper() and len(name) > 3:
        out.append(Suggestion("name", display_case(name), "The name is all upper-case.", 0.9))
    stripped = " ".join(name.split())
    if name and stripped != name:
        out.append(Suggestion("name", stripped, "The name has extra whitespace.", 0.95))
    if not values.get("category") and values.get("core_name") and category_by_core:
        cat = category_by_core.get(values["core_name"])
        if cat:
            out.append(Suggestion("category", cat, "Products with the same name are in this category.", 0.75))
    cat = values.get("category")
    if cat and category_spelling:
        preferred = category_spelling.get(brand_key(cat))
        if preferred and preferred != cat:
            out.append(Suggestion("category", preferred, "The catalog mostly spells this category differently.",
                                  0.85))
    if not values.get("brand") and name:
        guessed = parenthesised_brand(name)
        if guessed:
            out.append(Suggestion("brand", guessed, "Brand found in the product name.", 0.8))
    attrs = values.get("attributes") or {}
    for key, value in extract_attributes(name).items():
        if key not in attrs:
            out.append(Suggestion(f"attributes.{key}", value, f"'{key}' extracted from the product name.", 0.8))
    if not values.get("size") and name:
        q = parse_quantity(name)
        if q:
            out.append(Suggestion("size", format_quantity(q), "Pack size parsed from the product name.", 0.8))
    return out


def pairwise_metrics(clusters: Iterable[Sequence], truth: Iterable[Sequence]) -> dict:
    """Pairwise precision / recall / F1 of predicted clusters against true clusters."""
    from apps.customers.matching import pairwise_metrics as pm

    return pm(clusters, truth)
