"""Final-pass PII redactor applied to EVERY prompt sent to Gemini (defence in depth, AI_ARCHITECTURE §5.3.4).

Masks e-mail addresses, card-like numbers (Luhn-valid 13-19 digits), Aadhaar-like 12-digit numbers,
Indian PAN-like identifiers and phone numbers. Column-name aliasing for PII-tagged columns is done
by the prompt builders (they know the schema); this module only sees text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
CARD_CANDIDATE_RE = re.compile(r"(?<![\d\w])\d(?:[ -]?\d){12,18}(?![\d\w])")
AADHAAR_RE = re.compile(r"(?<![\d\w])(?<!\d[ -])[2-9]\d{3}([ -]?)\d{4}\1\d{4}(?![\d\w])(?![ -]\d)")
PAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")
PHONE_CANDIDATE_RE = re.compile(r"(?<![\w.])(\+?\(?\d[\d\s().-]{7,18}\d)(?![\w.])")

TOKENS = {
    "email": "[EMAIL]",
    "card": "[CARD]",
    "aadhaar": "[AADHAAR]",
    "pan": "[PAN]",
    "phone": "[PHONE]",
}


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_phone(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if not 10 <= len(digits) <= 15:
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", candidate.strip()):
        return False  # ISO date-ish
    if candidate.startswith("+"):
        return True
    has_sep = bool(re.search(r"[\s().-]", candidate))
    if has_sep:
        # e.g. 022-2345-6789, (555) 123-4567, 98765 43210
        return bool(re.fullmatch(r"\(?\d{2,5}\)?[\s.-]?\d{3,5}[\s.-]?\d{3,5}", candidate.strip()))
    # Contiguous: Indian mobile (10 digits starting 6-9), optionally prefixed by 0 or 91
    return bool(re.fullmatch(r"(?:0|91)?[6-9]\d{9}", digits))


def redact(text: str) -> RedactionResult:
    """Return the redacted text and per-kind counts."""
    if not text:
        return RedactionResult(text or "", {})
    counts: dict[str, int] = {}

    def bump(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return TOKENS[kind]

    out = EMAIL_RE.sub(lambda m: bump("email"), text)

    def card_sub(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            return bump("card")
        return m.group(0)

    out = CARD_CANDIDATE_RE.sub(card_sub, out)
    out = AADHAAR_RE.sub(lambda m: bump("aadhaar"), out)
    out = PAN_RE.sub(lambda m: bump("pan"), out)

    def phone_sub(m: re.Match) -> str:
        return bump("phone") if _is_phone(m.group(1)) else m.group(0)

    out = PHONE_CANDIDATE_RE.sub(phone_sub, out)
    return RedactionResult(out, counts)
