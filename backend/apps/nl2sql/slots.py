"""Question normalisation and slot extraction (numbers + time periods) for caching and re-binding."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from .timephrases import Period, find_periods

_PUNCT_RE = re.compile(r"[^\w\s.\-/:%]")
_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
NUM_TOKEN = "<num>"  # noqa: S105 - template token
PERIOD_TOKEN = "<period>"  # noqa: S105 - template token


def normalize_question(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower().strip()
    t = t.replace("’", "'").replace("'s ", " ")
    t = _PUNCT_RE.sub(" ", t)
    t = re.sub(r"(?<!\d)[.](?!\d)", " ", t)  # dots that are not decimal points
    t = re.sub(r"\s+", " ", t).strip(" .-/:")
    return t


def question_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Slots:
    normalized: str
    template: str
    numbers: tuple[float, ...] = ()
    periods: tuple[Period, ...] = ()
    period_spans: tuple[tuple[int, int], ...] = field(default_factory=tuple)

    @property
    def hash(self) -> str:
        return question_hash(self.normalized)

    @property
    def shape(self) -> str:
        return f"n{len(self.numbers)}p{len(self.periods)}"

    def values(self) -> dict[str, object]:
        """Slot name -> value: num_i (int|float), p{i}_start / p{i}_end (ISO date or None)."""
        out: dict[str, object] = {}
        for i, n in enumerate(self.numbers):
            out[f"num_{i}"] = int(n) if float(n).is_integer() else float(n)
        for i, p in enumerate(self.periods):
            out[f"p{i}_start"] = p.start.isoformat() if p.start else None
            out[f"p{i}_end"] = p.end.isoformat() if p.end else None
        return out


def extract_slots(question: str, today: date) -> Slots:
    norm = normalize_question(question)
    matches = find_periods(norm, today)
    pieces: list[str] = []
    last = 0
    for m in matches:
        pieces.append(norm[last:m.start_idx])
        pieces.append(f" {PERIOD_TOKEN} ")
        last = m.end_idx
    pieces.append(norm[last:])
    without_periods = "".join(pieces)
    numbers: list[float] = []

    def num_sub(mm: re.Match) -> str:
        numbers.append(float(mm.group(1)))
        return NUM_TOKEN

    template = _NUM_RE.sub(num_sub, without_periods)
    template = re.sub(r"\s+", " ", template).strip()
    return Slots(
        normalized=norm,
        template=template,
        numbers=tuple(numbers),
        periods=tuple(m.period for m in matches),
        period_spans=tuple((m.start_idx, m.end_idx) for m in matches),
    )
