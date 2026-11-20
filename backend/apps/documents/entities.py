"""Rule-based entity extraction (local, no AI): emails, phone numbers, dates, INR/USD amounts, PAN, GSTIN,
Aadhaar-like ids (Verhoeff-checked) and organisation names (suffix heuristic). Personal identifiers are flagged
`is_pii`. Overlapping matches are resolved by priority (more specific patterns first)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

MAX_ENTITIES_PER_PAGE = 300


@dataclass(frozen=True)
class Entity:
    kind: str
    value: str
    normalized: str
    start: int
    end: int
    is_pii: bool
    confidence: float = 1.0


EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}(?![\w-])")
GSTIN_RE = re.compile(r"(?<![A-Z0-9])\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z](?![A-Z0-9])")
PAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z](?![A-Z0-9])")
AADHAAR_RE = re.compile(r"(?<![\d-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d-])")
PHONE_RES = (
    re.compile(r"(?<![\w+])(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),                    # India mobile
    re.compile(r"(?<![\w+])\+\d{1,3}[\s-]?\(?\d{1,4}\)?(?:[\s-]?\d{2,4}){2,4}(?!\d)"),         # international
    re.compile(r"(?<!\d)\(\d{3}\)\s?\d{3}-\d{4}(?!\d)"),                                       # US (555) 010-0100
)
_MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|"
           "september|october|november|december")
DATE_RES = (
    (re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)"), "dmy"),
    (re.compile(rf"(?<!\w)(\d{{1,2}})(?:st|nd|rd|th)?[\s-]+({_MONTHS})\.?[\s,-]+(\d{{4}})(?!\d)", re.I), "d_mon_y"),
    (re.compile(rf"(?<!\w)({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.I), "mon_d_y"),
)
_NUM = r"\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_SCALE = r"(?:\s*(lakh|lakhs|lac|crore|crores|cr|million|mn|billion|bn|k))?"
AMOUNT_RES = (
    (re.compile(rf"(?:₹|\bRs\.?|\bINR)\s?({_NUM}){_SCALE}(?![\w])", re.I), "INR"),
    (re.compile(rf"(?<![\w.])({_NUM}){_SCALE}\s?(?:INR|rupees|/-)(?![\w])", re.I), "INR"),
    (re.compile(rf"(?:US\$|\$|\bUSD)\s?({_NUM}){_SCALE}(?![\w])", re.I), "USD"),
    (re.compile(rf"(?<![\w.])({_NUM}){_SCALE}\s?(?:USD|dollars)(?![\w])", re.I), "USD"),
)
_SCALES = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7, "million": 1e6,
           "mn": 1e6, "billion": 1e9, "bn": 1e9, "k": 1e3}
ORG_SUFFIX = (r"(?:Private\s+Limited|Pvt\.?\s*Ltd\.?|Limited|Ltd\.?|LLP|LLC|Inc\.?|Incorporated|Corporation|Corp\.?|"
              r"GmbH|PLC|Co\.|Company|Technologies|Solutions|Industries|Enterprises|Bank|Foundation|Trust)")
ORG_RE = re.compile(rf"(?<![\w&])((?:[A-Z][\w&'.-]*\s+){{1,5}}{ORG_SUFFIX})(?![\w])")

# Verhoeff tables (Aadhaar check digit)
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_valid(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def _month(token: str) -> int:
    return datetime.strptime(token[:3].title(), "%b").month


def normalize_date(match: re.Match, style: str) -> str | None:
    try:
        if style == "ymd":
            y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        elif style == "dmy":  # India: day first
            d, m, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        elif style == "d_mon_y":
            d, m, y = int(match.group(1)), _month(match.group(2)), int(match.group(3))
        else:
            m, d, y = _month(match.group(1)), int(match.group(2)), int(match.group(3))
        if not 1900 <= y <= 2100:
            return None
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def normalize_amount(number: str, scale: str | None, currency: str) -> str | None:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    if scale:
        value *= _SCALES.get(scale.lower(), 1)
    return f"{currency} {value:.2f}"


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def extract_entities(text: str) -> list[Entity]:
    if not text:
        return []
    found: list[Entity] = []
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in taken)

    def add(entity: Entity) -> None:
        if free(entity.start, entity.end):
            found.append(entity)
            taken.append((entity.start, entity.end))

    for m in EMAIL_RE.finditer(text):
        add(Entity("email", m.group(0), m.group(0).lower(), m.start(), m.end(), True))
    for m in GSTIN_RE.finditer(text):
        add(Entity("gstin", m.group(0), m.group(0), m.start(), m.end(), False))
    for m in PAN_RE.finditer(text):
        add(Entity("pan", m.group(0), m.group(0), m.start(), m.end(), True))
    for rx, style in DATE_RES:
        for m in rx.finditer(text):
            iso = normalize_date(m, style)
            if iso:
                add(Entity("date", m.group(0), iso, m.start(), m.end(), False))
    for rx, currency in AMOUNT_RES:
        for m in rx.finditer(text):
            norm = normalize_amount(m.group(1), m.group(2), currency)
            if norm:
                add(Entity("amount", m.group(0).strip(), norm, m.start(), m.end(), False))
    for m in AADHAAR_RE.finditer(text):
        digits = _digits(m.group(0))
        if len(digits) == 12 and verhoeff_valid(digits):
            add(Entity("aadhaar", m.group(0), digits, m.start(), m.end(), True, 0.9))
    for rx in PHONE_RES:
        for m in rx.finditer(text):
            digits = _digits(m.group(0))
            if 10 <= len(digits) <= 15:
                add(Entity("phone", m.group(0).strip(), "+" + digits if m.group(0).startswith("+") else digits,
                           m.start(), m.end(), True, 0.85))
    for m in ORG_RE.finditer(text):
        value = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(value.split()) >= 2:
            add(Entity("org", value, value, m.start(1), m.end(1), False, 0.7))
    found.sort(key=lambda e: e.start)
    return found[:MAX_ENTITIES_PER_PAGE]
