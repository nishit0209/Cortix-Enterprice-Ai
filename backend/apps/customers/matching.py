"""Customer identity resolution, pure functions (no DB): normalization, blocking, pairwise scoring, survivorship,
RFM and rule segments. `apps.customers.services` runs them over the org's source records.

Normalization
    email   trimmed + lower-cased; `email_key` also drops dots and a "+tag" in the local part ("a.b+x@d" -> "ab@d")
    phone   digits only, E.164-ish: national numbers get the org's default country code ("+91 98765 43210",
            "098765 43210", "+91-9876543210" and "987-654-3210" all become "+919876543210")
    name    NFKC/NFKD folded, accents and punctuation removed, "Last, First" reordered; tokens kept for comparisons
    address tokens (lower-cased words, known city renames canonicalized: bombay -> mumbai, bangalore -> bengaluru ...)
    dob     ISO date (YYYY-MM-DD) from common formats

Blocking keys (records sharing any key are compared)
    email domain + first letter of the local part, the email key, the last 7 phone digits, and a Soundex key of the
    name tokens (order independent).

Pairwise features (each in [0, 1], None when a side has no value)
    email   1.0 exact, 0.95 same email key, else 0.0
    phone   1.0 same E.164, 0.9 same last 10 digits, 0.5 same last 7 digits, else 0.0
    name    max(Jaro-Winkler, token-sort ratio) of the normalized names (word order and typos tolerated)
    address token-set ratio of the address tokens
    dob     1.0 equal, else 0.0

Score = weighted mean of the available features (DEFAULT_WEIGHTS), scaled down when the evidence covers less than
half of the total weight, then deterministic rules: same email + similar name (>= 0.7) -> at least 0.97; same phone +
similar name (>= 0.8) -> at least 0.95; different dates of birth -> x0.7; both email and phone present and different
-> at most 0.6. Thresholds (per org, `CustomerSettings`): score >= AUTO (default 0.90) auto-merges, REVIEW (default
0.70) <= score < AUTO goes to the review queue, below REVIEW is no match.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from apps.quality.services import fold, soundex

FIELDS = ("email", "phone", "name", "first_name", "last_name", "address", "dob", "external_id", "created_at")
GOLDEN_FIELDS = ("name", "email", "phone", "address", "dob", "created_at")
DEFAULT_WEIGHTS = {"email": 0.35, "phone": 0.30, "name": 0.20, "address": 0.08, "dob": 0.07}
DEFAULT_AUTO = 0.90
DEFAULT_REVIEW = 0.70
DEFAULT_COUNTRY_CODE = "91"
SURVIVORSHIP_RULES = ("most_recent", "most_frequent", "source_priority", "earliest", "longest")
DEFAULT_SURVIVORSHIP = {
    "name": "most_frequent", "email": "source_priority", "phone": "most_frequent", "address": "most_recent",
    "dob": "most_frequent", "created_at": "earliest",
}
CITY_ALIASES = {
    "bombay": "mumbai", "bangalore": "bengaluru", "madras": "chennai", "calcutta": "kolkata", "poona": "pune",
    "gurgaon": "gurugram", "baroda": "vadodara", "trivandrum": "thiruvananthapuram", "cochin": "kochi",
    "mysore": "mysuru", "benares": "varanasi", "pondicherry": "puducherry", "simla": "shimla", "vizag": "visakhapatnam",
    "allahabad": "prayagraj", "orissa": "odisha", "new delhi": "delhi",
}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d", "%d %b %Y",
                 "%d %B %Y", "%b %d, %Y", "%B %d, %Y")


# ----------------------------------------------------------------------------------------------- normalization
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def normalize_email(value: Any) -> tuple[str | None, str | None]:
    """-> (email, email_key) or (None, None) when the value is not an e-mail address."""
    email = _text(value).lower()
    if not email or not _EMAIL_RE.match(email):
        return None, None
    local, domain = email.rsplit("@", 1)
    key_local = local.split("+", 1)[0].replace(".", "")
    return email, f"{key_local}@{domain}"


def normalize_phone(value: Any, default_country: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """E.164-ish "+<country><national>"; None for fewer than 7 digits."""
    raw = _text(value)
    if not raw:
        return None
    if isinstance(value, float) and value.is_integer():
        raw = str(int(value))
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits, has_plus = digits[2:], True
    if len(digits) < 7:
        return None
    cc = re.sub(r"\D", "", default_country or "") or DEFAULT_COUNTRY_CODE
    if has_plus:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 11:  # national trunk prefix
        digits = digits[1:]
    if len(digits) == 10:
        return f"+{cc}{digits}"
    if digits.startswith(cc) and len(digits) == len(cc) + 10:
        return "+" + digits
    return "+" + digits


def name_tokens(value: Any) -> list[str]:
    text = _text(value)
    if not text:
        return []
    if "," in text:  # "Last, First" -> "First Last"
        last, _, first = text.partition(",")
        text = f"{first} {last}"
    folded = _PUNCT_RE.sub(" ", fold(text))
    return [t for t in folded.split() if t]


def normalize_name(value: Any = None, *, first: Any = None, last: Any = None) -> str | None:
    if value is None or _text(value) == "":
        value = " ".join(p for p in (_text(first), _text(last)) if p)
    tokens = name_tokens(value)
    return " ".join(tokens) or None


def address_tokens(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        value = " ".join(_text(v) for v in value if _text(v))
    text = _PUNCT_RE.sub(" ", fold(_text(value)))
    text = " ".join(text.split())
    for old, new in CITY_ALIASES.items():
        text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    return text.split()


def normalize_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = _text(value)
    if not text:
        return None
    head = text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else text
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(head, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_timestamp(value: Any) -> dt.datetime | None:
    """A timezone-aware datetime (UTC for naive values) from a date/datetime/ISO string, else None."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)
    text = _text(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        day = normalize_date(text)
        return parse_timestamp(dt.date.fromisoformat(day)) if day else None


def normalize_record(raw: Mapping[str, Any], *, default_country: str = DEFAULT_COUNTRY_CODE) -> dict:
    """Mapped source values (field -> value) -> normalized fields used for blocking, matching and golden records."""
    email, email_key = normalize_email(raw.get("email"))
    name = normalize_name(raw.get("name"), first=raw.get("first_name"), last=raw.get("last_name"))
    display = _text(raw.get("name")) or " ".join(p for p in (_text(raw.get("first_name")),
                                                             _text(raw.get("last_name"))) if p)
    if "," in display:
        last, _, first = display.partition(",")
        display = f"{first.strip()} {last.strip()}".strip()
    address = address_tokens(raw.get("address"))
    raw_address = raw.get("address")
    if isinstance(raw_address, list | tuple):
        address_display = ", ".join(_text(v) for v in raw_address if _text(v)) or None
    else:
        address_display = _text(raw_address) or None
    created = parse_timestamp(raw.get("created_at"))
    return {
        "email": email, "email_key": email_key,
        "phone": normalize_phone(raw.get("phone"), default_country),
        "name": name, "display_name": display or None,
        "address": " ".join(address) or None,
        "address_display": address_display,
        "dob": normalize_date(raw.get("dob")),
        "created_at": created.isoformat() if created else None,
    }


def source_key(raw: Mapping[str, Any]) -> str:
    """The record's key in its source: the mapped external_id, else a stable hash of the mapped values."""
    ext = _text(raw.get("external_id"))
    if ext:
        return ext[:255]
    digest = hashlib.sha1(repr(sorted((k, _text(v)) for k, v in raw.items())).encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"row:{digest[:32]}"


# ----------------------------------------------------------------------------------------------- blocking
def blocking_keys(norm: Mapping[str, Any]) -> list[str]:
    keys = []
    email = norm.get("email")
    if email:
        local, domain = email.rsplit("@", 1)
        keys.append(f"ed:{domain}:{local[:1]}")
    if norm.get("email_key"):
        keys.append(f"ek:{norm['email_key']}")
    phone = norm.get("phone")
    if phone:
        digits = re.sub(r"\D", "", phone)
        keys.append(f"ph:{digits[-7:]}")
    tokens = (norm.get("name") or "").split()
    if tokens:
        codes = sorted({soundex(t) for t in tokens if soundex(t)})
        if len(codes) >= 2:
            keys.append(f"nm:{codes[0]}:{codes[-1]}")
        elif codes:
            keys.append(f"nm:{codes[0]}")
    return keys


# ----------------------------------------------------------------------------------------------- scoring
def _email_sim(a: Mapping, b: Mapping) -> float | None:
    if not a.get("email") or not b.get("email"):
        return None
    if a["email"] == b["email"]:
        return 1.0
    if a.get("email_key") and a.get("email_key") == b.get("email_key"):
        return 0.95
    return 0.0


def _phone_sim(a: Mapping, b: Mapping) -> float | None:
    pa, pb = a.get("phone"), b.get("phone")
    if not pa or not pb:
        return None
    if pa == pb:
        return 1.0
    da, db = re.sub(r"\D", "", pa), re.sub(r"\D", "", pb)
    if da[-10:] == db[-10:] and len(da) >= 10 and len(db) >= 10:
        return 0.9
    if da[-7:] == db[-7:]:
        return 0.5
    return 0.0


def name_similarity(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    if a == b:
        return 1.0
    jw = JaroWinkler.normalized_similarity(a, b)
    tsr = fuzz.token_sort_ratio(a, b) / 100.0
    return round(max(jw, tsr), 4)


def _address_sim(a: Mapping, b: Mapping) -> float | None:
    if not a.get("address") or not b.get("address"):
        return None
    return round(fuzz.token_set_ratio(a["address"], b["address"]) / 100.0, 4)


def _dob_sim(a: Mapping, b: Mapping) -> float | None:
    if not a.get("dob") or not b.get("dob"):
        return None
    return 1.0 if a["dob"] == b["dob"] else 0.0


@dataclass(frozen=True)
class PairScore:
    score: float
    features: dict = field(default_factory=dict)
    rules: tuple[str, ...] = ()


def score_pair(a: Mapping[str, Any], b: Mapping[str, Any], weights: Mapping[str, float] | None = None) -> PairScore:
    """Weighted, rule-adjusted match score of two normalized records (see the module docstring)."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    feats = {
        "email": _email_sim(a, b), "phone": _phone_sim(a, b), "name": name_similarity(a.get("name"), b.get("name")),
        "address": _address_sim(a, b), "dob": _dob_sim(a, b),
    }
    total = sum(max(0.0, float(w)) for w in weights.values()) or 1.0
    present = {k: v for k, v in feats.items() if v is not None and weights.get(k, 0) > 0}
    covered = sum(float(weights[k]) for k in present)
    if not present or covered <= 0:
        return PairScore(0.0, {**feats, "coverage": 0.0}, ())
    score = sum(float(weights[k]) * v for k, v in present.items()) / covered
    coverage = covered / total
    if coverage < 0.5:
        score *= coverage / 0.5
    rules: list[str] = []
    name = feats["name"] or 0.0
    if feats["email"] == 1.0 and name >= 0.7:
        score, rules = max(score, 0.97), [*rules, "same_email_similar_name"]
    if feats["phone"] == 1.0 and name >= 0.8:
        score, rules = max(score, 0.95), [*rules, "same_phone_similar_name"]
    if feats["dob"] == 0.0:
        score, rules = score * 0.7, [*rules, "different_dob"]
    if feats["email"] == 0.0 and feats["phone"] == 0.0:
        score, rules = min(score, 0.6), [*rules, "different_email_and_phone"]
    return PairScore(round(min(max(score, 0.0), 1.0), 4), {**feats, "coverage": round(coverage, 4)}, tuple(rules))


def decide(score: float, *, auto: float = DEFAULT_AUTO, review: float = DEFAULT_REVIEW) -> str | None:
    """"auto_merged" | "pending_review" | None (no match)."""
    if score >= auto:
        return "auto_merged"
    if score >= review:
        return "pending_review"
    return None


def validate_thresholds(auto: float, review: float) -> None:
    if not (0 < review < auto <= 1):
        raise ValueError("Thresholds must satisfy 0 < review < auto <= 1.")


# ----------------------------------------------------------------------------------------------- survivorship
@dataclass(frozen=True)
class Candidate:
    """One record's contribution to a golden record."""

    record_id: Any
    source_system: str
    priority: int  # lower = more trusted
    recorded_at: str | None  # ISO timestamp used by "most_recent"
    values: Mapping[str, Any]


def _pick(field_name: str, rule: str, candidates: Sequence[Candidate]) -> tuple[Any, Candidate | None]:
    have = [c for c in candidates if c.values.get(field_name) not in (None, "")]
    if not have:
        return None, None
    if rule == "most_frequent":
        counts = Counter(_comparable(c.values[field_name]) for c in have)
        best = max(counts.values())
        tied = [c for c in have if counts[_comparable(c.values[field_name])] == best]
        chosen = sorted(tied, key=lambda c: (c.priority, _neg_time(c.recorded_at), str(c.record_id)))[0]
    elif rule == "source_priority":
        chosen = sorted(have, key=lambda c: (c.priority, _neg_time(c.recorded_at), str(c.record_id)))[0]
    elif rule == "earliest":
        chosen = sorted(have, key=lambda c: (str(c.values[field_name]), c.priority, str(c.record_id)))[0]
    elif rule == "longest":
        chosen = sorted(have, key=lambda c: (-len(str(c.values[field_name])), c.priority, str(c.record_id)))[0]
    else:  # most_recent (default): the newest record with a value, then source priority
        chosen = sorted(have, key=lambda c: (_neg_time(c.recorded_at), c.priority, str(c.record_id)))[0]
    return chosen.values[field_name], chosen


def _comparable(value: Any) -> str:
    return " ".join(fold(str(value)).split())


def _neg_time(value: str | None) -> tuple[int, str]:
    # sorts newest first; records without a timestamp come last
    if not value:
        return (1, "")
    inverted = "".join(chr(0x10FFFF - ord(ch)) if ord(ch) < 0x10FFFF else ch for ch in value)
    return (0, inverted)


def golden_record(candidates: Sequence[Candidate], rules: Mapping[str, str] | None = None) -> tuple[dict, dict]:
    """-> (golden values, survivorship {field: {rule, record_id, source}}) for one cluster."""
    rules = {**DEFAULT_SURVIVORSHIP, **(rules or {})}
    golden: dict[str, Any] = {}
    trail: dict[str, Any] = {}
    for f in GOLDEN_FIELDS:
        rule = rules.get(f, "most_recent")
        value, chosen = _pick(f, rule, candidates)
        golden[f] = value
        if chosen is not None:
            trail[f] = {"rule": rule, "record_id": str(chosen.record_id), "source": chosen.source_system}
    ids: dict[str, list[str]] = {}
    for c in candidates:
        ext = c.values.get("external_id")
        if ext:
            ids.setdefault(c.source_system, [])
            if ext not in ids[c.source_system]:
                ids[c.source_system].append(ext)
    golden["source_ids"] = ids
    return golden, trail


# ----------------------------------------------------------------------------------------------- RFM
@dataclass(frozen=True)
class RfmRow:
    profile_id: Any
    recency_days: int
    frequency: int
    monetary: float
    r: int
    f: int
    m: int
    label: str

    @property
    def code(self) -> str:
        return f"{self.r}{self.f}{self.m}"


RFM_LABELS = ("Champions", "Loyal customers", "Potential loyalists", "New customers", "Need attention", "At risk",
              "Hibernating")


def quintile_scores(values: Sequence[float], *, higher_is_better: bool = True) -> list[int]:
    """1..5 scores by rank (ties share the lowest rank of their value), 5 = best. Robust to heavy ties."""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = i  # tie -> first position
        i = j + 1
    scores = [min(5, 1 + (5 * ranks[k]) // n) for k in range(n)]
    return scores if higher_is_better else [6 - s for s in scores]


def rfm_label(r: int, f: int) -> str:
    if r >= 4 and f >= 4:
        return "Champions"
    if f >= 4:
        return "Loyal customers" if r >= 3 else "At risk"
    if r >= 4 and f >= 2:
        return "Potential loyalists"
    if r >= 4:
        return "New customers"
    if r <= 2 and f >= 3:
        return "At risk"
    if r <= 2:
        return "Hibernating"
    return "Need attention"


def compute_rfm(stats: Iterable[tuple[Any, dt.datetime, int, float]], as_of: dt.datetime) -> list[RfmRow]:
    """stats: (profile_id, last order time, order count, monetary). Recency in days; quintile R/F/M scores."""
    rows = [(pid, max(0, (as_of - last).days), int(freq), float(mon)) for pid, last, freq, mon in stats
            if last is not None and freq > 0]
    if not rows:
        return []
    r_scores = quintile_scores([r[1] for r in rows], higher_is_better=False)
    f_scores = quintile_scores([r[2] for r in rows])
    m_scores = quintile_scores([r[3] for r in rows])
    return [
        RfmRow(pid, rec, freq, round(mon, 2), r, f, m, rfm_label(r, f))
        for (pid, rec, freq, mon), r, f, m in zip(rows, r_scores, f_scores, m_scores, strict=True)
    ]


# ----------------------------------------------------------------------------------------------- rule segments
SEGMENT_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "is_null", "not_null"}
OP_ALIASES = {"=": "eq", "==": "eq", "!=": "ne", "<>": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}
SEGMENT_FIELDS = {"record_count", "event_count", "order_count", "monetary", "recency_days", "first_seen", "last_seen",
                  "sources", "rfm_label", "r", "f", "m"}
GOLDEN_PREFIX = "golden."


def validate_conditions(definition: Mapping[str, Any]) -> dict:
    """Normalize a rule-segment definition {match: all|any, conditions: [{field, op, value}]}; raises ValueError."""
    if not isinstance(definition, Mapping):
        raise ValueError("The definition must be an object.")
    match = str(definition.get("match") or "all")
    if match not in ("all", "any"):
        raise ValueError("match must be 'all' or 'any'.")
    conds = definition.get("conditions")
    if not isinstance(conds, list) or not conds:
        raise ValueError("At least one condition is required.")
    if len(conds) > 20:
        raise ValueError("At most 20 conditions.")
    out = []
    for i, cond in enumerate(conds):
        if not isinstance(cond, Mapping):
            raise ValueError(f"Condition {i + 1} must be an object.")
        fld = str(cond.get("field") or "")
        op = OP_ALIASES.get(str(cond.get("op") or ""), str(cond.get("op") or ""))
        if fld not in SEGMENT_FIELDS and not (fld.startswith(GOLDEN_PREFIX) and fld[len(GOLDEN_PREFIX):]
                                              in GOLDEN_FIELDS):
            raise ValueError(f"Condition {i + 1}: unknown field {fld!r}.")
        if op not in SEGMENT_OPS:
            raise ValueError(f"Condition {i + 1}: unknown operator {cond.get('op')!r}.")
        value = cond.get("value")
        if op in ("in", "not_in") and not isinstance(value, list):
            raise ValueError(f"Condition {i + 1}: {op} needs a list value.")
        if op not in ("is_null", "not_null") and value is None:
            raise ValueError(f"Condition {i + 1}: a value is required.")
        out.append({"field": fld, "op": op, "value": value})
    return {"match": match, "conditions": out}


def _coerce(left: Any, right: Any) -> tuple[Any, Any]:
    if isinstance(left, dt.datetime | dt.date):
        left = left.isoformat()
    if isinstance(left, Decimal):
        left = float(left)
    if isinstance(left, bool) or left is None:
        return left, right
    if isinstance(left, int | float):
        try:
            return float(left), float(right)
        except (TypeError, ValueError):
            return str(left), str(right)
    return fold(str(left)), fold(str(right)) if right is not None else None


def _check(value: Any, op: str, target: Any) -> bool:
    if op == "is_null":
        return value in (None, "", [])
    if op == "not_null":
        return value not in (None, "", [])
    if value in (None, ""):
        return False
    if op == "contains":
        if isinstance(value, list | tuple):
            return fold(str(target)) in {fold(str(v)) for v in value}
        return fold(str(target)) in fold(str(value))
    if op in ("in", "not_in"):
        member = any(_coerce(value, t)[0] == _coerce(value, t)[1] for t in target)
        return member if op == "in" else not member
    left, right = _coerce(value, target)
    try:
        return {"eq": left == right, "ne": left != right, "gt": left > right, "gte": left >= right,
                "lt": left < right, "lte": left <= right}[op]
    except TypeError:
        return False


def matches_definition(row: Mapping[str, Any], definition: Mapping[str, Any]) -> bool:
    """row: profile facts (SEGMENT_FIELDS) plus `golden` (dict)."""
    results = []
    for cond in definition["conditions"]:
        fld = cond["field"]
        value = (row.get("golden") or {}).get(fld[len(GOLDEN_PREFIX):]) if fld.startswith(GOLDEN_PREFIX) else row.get(fld)
        results.append(_check(value, cond["op"], cond["value"]))
    return all(results) if definition.get("match", "all") == "all" else any(results)


# ----------------------------------------------------------------------------------------------- resolution
@dataclass
class Resolution:
    """Outcome of one resolution pass over all records."""

    matches: dict[tuple, tuple[PairScore, str]]  # pair_key -> (score, auto_merged | pending_review)
    clusters: list[list]                          # record ids per entity (singletons included)
    stats: dict


def resolve(records: Mapping[Any, Mapping[str, Any]], *, weights: Mapping[str, float] | None = None,
            auto: float = DEFAULT_AUTO, review: float = DEFAULT_REVIEW, confirmed: Iterable[tuple] = (),
            rejected: Iterable[tuple] = (), max_block: int = 1000) -> Resolution:
    """Block, score and cluster normalized records (record id -> normalized fields).

    Confirmed pairs are merged first, rejected pairs are cannot-links (never merged, not even transitively), then
    auto-merge pairs join clusters by descending score. Pending-review pairs never merge until a reviewer confirms.
    """
    from apps.quality.services import UnionFind, candidate_pairs, pair_key

    validate_thresholds(auto, review)
    keys = {rid: blocking_keys(norm) for rid, norm in records.items()}
    pairs, block_stats = candidate_pairs(keys, max_block=max_block)
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
    blocked_merges = 0
    for pair, (_result, status) in sorted(matches.items(), key=lambda kv: (-kv[1][0].score, str(kv[0]))):
        if status == "auto_merged" and not uf.union(*pair):
            blocked_merges += 1
    clusters = uf.groups()
    stats = {
        "records": len(records), "candidate_pairs": len(pairs), **{f"blocking_{k}": v for k, v in
                                                                  block_stats.as_dict().items() if k != "pairs"},
        "auto_merged": sum(1 for _, s in matches.values() if s == "auto_merged"),
        "pending_review": sum(1 for _, s in matches.values() if s == "pending_review"),
        "confirmed": len(confirmed), "rejected": len(rejected), "merges_blocked_by_rejections": blocked_merges,
        "entities": len(clusters), "multi_record_entities": sum(1 for c in clusters if len(c) > 1),
    }
    return Resolution(matches, clusters, stats)


def pairwise_metrics(clusters: Iterable[Sequence], truth: Iterable[Sequence]) -> dict:
    """Pairwise precision / recall / F1 of predicted clusters against true clusters (record ids)."""
    def pairs_of(groups):
        out = set()
        for g in groups:
            g = sorted(map(str, g))
            out.update((g[i], g[j]) for i in range(len(g)) for j in range(i + 1, len(g)))
        return out

    predicted, actual = pairs_of(clusters), pairs_of(truth)
    tp = len(predicted & actual)
    precision = tp / len(predicted) if predicted else 1.0
    recall = tp / len(actual) if actual else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"true_positives": tp, "predicted_pairs": len(predicted), "true_pairs": len(actual),
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}
