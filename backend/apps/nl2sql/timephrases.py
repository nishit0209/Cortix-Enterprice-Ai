"""Time-phrase recognition for NL questions ("last month", "past 30 days", "in March 2026", "since 1 Jan 2026",
"Q3 2025", "between 2026-01-01 and 2026-03-31", ...).

Calendar-aligned periods are computed locally; absolute dates are parsed with `dateparser`.
Every Period is half-open: start <= d < end (either bound may be None for open ranges).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import dateparser

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
UNITS = {"day": "day", "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month",
         "quarter": "quarter", "quarters": "quarter", "year": "year", "years": "year"}


@dataclass(frozen=True)
class Period:
    start: date | None
    end: date | None  # exclusive
    grain: str        # day | week | month | quarter | year | custom
    phrase: str       # matched text
    label: str        # human label, e.g. "last month"

    @property
    def days(self) -> int | None:
        if self.start and self.end:
            return (self.end - self.start).days
        return None

    def describe(self) -> str:
        if self.start and self.end:
            return f"{self.label} ({self.start.isoformat()} to {(self.end - timedelta(days=1)).isoformat()})"
        if self.start:
            return f"{self.label} (from {self.start.isoformat()})"
        if self.end:
            return f"{self.label} (before {self.end.isoformat()})"
        return self.label


@dataclass(frozen=True)
class Match:
    start_idx: int
    end_idx: int
    period: Period


# ----------------------------------------------------------------------------------------- calendar math
def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31,
            30, 31][m - 1]
    return date(y, m, min(d.day, last))


def unit_start(d: date, unit: str) -> date:
    if unit == "day":
        return d
    if unit == "week":
        return d - timedelta(days=d.weekday())  # ISO week, Monday
    if unit == "month":
        return d.replace(day=1)
    if unit == "quarter":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    if unit == "year":
        return date(d.year, 1, 1)
    raise ValueError(unit)


def shift(d: date, unit: str, n: int) -> date:
    if unit == "day":
        return d + timedelta(days=n)
    if unit == "week":
        return d + timedelta(days=7 * n)
    if unit == "month":
        return add_months(d, n)
    if unit == "quarter":
        return add_months(d, 3 * n)
    if unit == "year":
        return add_months(d, 12 * n)
    raise ValueError(unit)


def calendar_period(today: date, unit: str, offset: int, phrase: str, label: str) -> Period:
    start = shift(unit_start(today, unit), unit, offset)
    return Period(start, shift(start, unit, 1), unit, phrase, label)


def _parse_date(text: str, today: date) -> date | None:
    text = text.strip().strip(",.")
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    parsed = dateparser.parse(
        text,
        languages=["en"],
        settings={
            "PREFER_DATES_FROM": "past",
            "RELATIVE_BASE": datetime(today.year, today.month, today.day),
            "DATE_ORDER": "DMY",
            "PREFER_DAY_OF_MONTH": "first",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    return parsed.date() if parsed else None


# ----------------------------------------------------------------------------------------- recognizers
_DATE_TOKEN = rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_ALT})(?:\s+\d{{4}})?|(?:{_MONTH_ALT})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?|(?:{_MONTH_ALT})\s+\d{{4}}|\d{{4}})"

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("between", re.compile(rf"\b(?:between|from)\s+({_DATE_TOKEN})\s+(?:and|to|until|till|-)\s+({_DATE_TOKEN})\b")),
    ("since", re.compile(rf"\b(?:since|after|from|starting)\s+({_DATE_TOKEN})\b")),
    ("before", re.compile(rf"\b(?:before|until|till|prior to)\s+({_DATE_TOKEN})\b")),
    ("rolling", re.compile(r"\b(?:in\s+the\s+|over\s+the\s+|for\s+the\s+|during\s+the\s+)?(?:last|past|previous|trailing)\s+(\d{1,4})\s+(days?|weeks?|months?|quarters?|years?)\b")),
    ("to_date", re.compile(r"\b(?:(month|quarter|year)[\s-]to[\s-]date|(mtd|qtd|ytd))\b")),
    ("relative", re.compile(r"\b(?:(?:in|during|for|over)\s+)?(this|current|last|previous|prior|next)\s+(week|month|quarter|year)\b")),
    ("day", re.compile(r"\b(today|yesterday)\b")),
    ("quarter", re.compile(r"\b(?:in\s+|during\s+|for\s+)?q([1-4])(?:\s+|\s*-\s*|\s*fy\s*)?(\d{4})?\b")),
    ("month", re.compile(rf"\b(?:in|during|for|of)\s+({_MONTH_ALT})(?:\s+(\d{{4}}))?\b")),
    ("year", re.compile(r"\b(?:in|during|for|of)\s+((?:19|20)\d{2})\b")),
]


def _period_for(kind: str, m: re.Match, today: date) -> Period | None:
    phrase = m.group(0).strip()
    if kind == "between":
        a, b = _parse_date(m.group(1), today), _parse_date(m.group(2), today)
        if not a or not b:
            return None
        if re.fullmatch(r"\d{4}", m.group(2).strip()):
            b = date(b.year, 12, 31)
        if a > b:
            a, b = b, a
        return Period(a, b + timedelta(days=1), "custom", phrase, f"between {a.isoformat()} and {b.isoformat()}")
    if kind == "since":
        a = _parse_date(m.group(1), today)
        return Period(a, today + timedelta(days=1), "custom", phrase, f"since {a.isoformat()}") if a else None
    if kind == "before":
        b = _parse_date(m.group(1), today)
        return Period(None, b, "custom", phrase, f"before {b.isoformat()}") if b else None
    if kind == "rolling":
        n, unit = int(m.group(1)), UNITS[m.group(2)]
        if n <= 0:
            return None
        end = today + timedelta(days=1)
        start = shift(end, unit, -n)
        return Period(start, end, "custom", phrase, f"the last {n} {unit}{'s' if n != 1 else ''}")
    if kind == "to_date":
        unit = m.group(1) or {"mtd": "month", "qtd": "quarter", "ytd": "year"}[m.group(2)]
        return Period(unit_start(today, unit), today + timedelta(days=1), unit, phrase, f"{unit} to date")
    if kind == "relative":
        which, unit = m.group(1), m.group(2)
        offset = {"this": 0, "current": 0, "last": -1, "previous": -1, "prior": -1, "next": 1}[which]
        label = {0: f"this {unit}", -1: f"last {unit}", 1: f"next {unit}"}[offset]
        return calendar_period(today, unit, offset, phrase, label)
    if kind == "day":
        d = today if m.group(1) == "today" else today - timedelta(days=1)
        return Period(d, d + timedelta(days=1), "day", phrase, m.group(1))
    if kind == "quarter":
        q = int(m.group(1))
        year = int(m.group(2)) if m.group(2) else today.year
        start = date(year, 3 * (q - 1) + 1, 1)
        if not m.group(2) and start > today:
            start = date(year - 1, start.month, 1)
        return Period(start, add_months(start, 3), "quarter", phrase, f"Q{q} {start.year}")
    if kind == "month":
        month = MONTHS[m.group(1)]
        if m.group(2):
            year = int(m.group(2))
        else:
            year = today.year if month <= today.month else today.year - 1
        start = date(year, month, 1)
        return Period(start, add_months(start, 1), "month", phrase, start.strftime("%B %Y"))
    if kind == "year":
        y = int(m.group(1))
        return Period(date(y, 1, 1), date(y + 1, 1, 1), "year", phrase, str(y))
    return None


def find_periods(text: str, today: date) -> list[Match]:
    """All non-overlapping time phrases in `text` (lower-cased), left to right."""
    found: list[Match] = []
    taken: list[tuple[int, int]] = []
    for kind, rx in _PATTERNS:
        for m in rx.finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            period = _period_for(kind, m, today)
            if period is None:
                continue
            found.append(Match(s, e, period))
            taken.append((s, e))
    found.sort(key=lambda x: x.start_idx)
    return found
