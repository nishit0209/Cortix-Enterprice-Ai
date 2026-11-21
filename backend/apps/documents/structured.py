"""Local, schema-driven field extraction (AI_ARCHITECTURE.md §7.9) for invoices, receipts and resumes.

Rules + regular expressions over the stored page text: no network and no LLM. Every value carries a citation
({page, start, end, quote}: offsets into that page's text); a field without a citation is null. The optional Gemini
path (apps.documents.services.extract) uses the same schemas and is policy-gated.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .entities import (
    DATE_RES,
    EMAIL_RE,
    GSTIN_RE,
    ORG_RE,
    PHONE_RES,
    normalize_date,
)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str  # string | date | amount | list | integer
    description: str


SCHEMAS: dict[str, dict] = {
    "invoice": {
        "label": "Invoice",
        "fields": [
            FieldSpec("invoice_number", "string", "Invoice number"),
            FieldSpec("invoice_date", "date", "Date of issue (ISO)"),
            FieldSpec("due_date", "date", "Payment due date (ISO)"),
            FieldSpec("seller_name", "string", "Issuing company"),
            FieldSpec("buyer_name", "string", "Billed party"),
            FieldSpec("gstin", "string", "GSTIN on the invoice"),
            FieldSpec("subtotal", "amount", "Amount before tax"),
            FieldSpec("tax_amount", "amount", "Total tax"),
            FieldSpec("total_amount", "amount", "Total / amount due"),
            FieldSpec("currency", "string", "ISO currency code"),
            FieldSpec("email", "string", "Contact email"),
            FieldSpec("phone", "string", "Contact phone"),
        ],
    },
    "receipt": {
        "label": "Receipt",
        "fields": [
            FieldSpec("receipt_number", "string", "Receipt / transaction number"),
            FieldSpec("date", "date", "Purchase date (ISO)"),
            FieldSpec("merchant", "string", "Merchant name"),
            FieldSpec("tax_amount", "amount", "Tax"),
            FieldSpec("total_amount", "amount", "Total paid"),
            FieldSpec("payment_method", "string", "cash | card | upi | wallet | net_banking | cod"),
            FieldSpec("currency", "string", "ISO currency code"),
        ],
    },
    "resume": {
        "label": "Resume",
        "fields": [
            FieldSpec("name", "string", "Candidate name"),
            FieldSpec("email", "string", "Email"),
            FieldSpec("phone", "string", "Phone"),
            FieldSpec("current_title", "string", "Current or target role"),
            FieldSpec("years_experience", "integer", "Total years of experience"),
            FieldSpec("skills", "list", "Skills"),
            FieldSpec("education", "list", "Degrees"),
            FieldSpec("linkedin", "string", "LinkedIn URL"),
        ],
    },
}


def schema_keys() -> list[str]:
    return list(SCHEMAS)


def field_names(schema_key: str) -> list[str]:
    return [f.name for f in SCHEMAS[schema_key]["fields"]]


# ----------------------------------------------------------------------------------------------- matching
_CUR = r"(?:₹|Rs\.?|INR|USD|US\$|\$|€|EUR|£|GBP)"
_AMOUNT = rf"({_CUR}?\s?\d[\d,]*(?:\.\d{{1,2}})?)(?!\s*%)(?![\d,])"
_SEP = r"\s*[:#\-]?\s*"
_DATE_ANY = (r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{1,2}(?:st|nd|rd|th)?[\s-]+[A-Za-z]{3,9}\.?"
             r"[\s,-]+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})")
_ID = r"([A-Z0-9][A-Z0-9/_\-]{2,40})"
PAYMENT_METHODS = (("upi", r"\bUPI\b"), ("net_banking", r"\bnet\s*banking\b"), ("cod", r"\b(?:COD|cash on delivery)\b"),
                   ("card", r"\b(?:credit|debit)?\s*card\b|\bVISA\b|\bMastercard\b|\bRuPay\b"),
                   ("wallet", r"\bwallet\b|\bPaytm\b"), ("cash", r"\bcash\b"))
DEGREE_RE = re.compile(r"\b(B\.?\s?Tech|B\.?\s?E\.?|M\.?\s?Tech|MBA|BBA|BCA|MCA|B\.?\s?Sc|M\.?\s?Sc|B\.?\s?Com|"
                       r"M\.?\s?Com|Ph\.?\s?D|Bachelor(?:'s)?(?: of [A-Za-z ]{3,40})?|Master(?:'s)?(?: of [A-Za-z ]{3,40})?|"
                       r"Diploma(?: in [A-Za-z ]{3,40})?)\b[^\n]{0,80}", re.I)
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_\-%]+/?", re.I)


@dataclass
class Found:
    value: object
    page: int
    start: int
    end: int
    quote: str
    confidence: float

    def citation(self) -> dict:
        return {"page": self.page, "start": self.start, "end": self.end, "quote": self.quote[:300]}


@dataclass
class Extraction:
    schema_key: str
    result: dict = field(default_factory=dict)
    citations: dict = field(default_factory=dict)
    confidence: dict = field(default_factory=dict)

    @property
    def fields_found(self) -> int:
        return sum(1 for v in self.result.values() if v not in (None, [], ""))


def _labelled(pages, labels: list[str], value: str, flags=re.I, sep: str = _SEP) -> tuple[re.Match, int] | None:
    for label in labels:
        rx = re.compile(rf"(?<![A-Za-z]){label}{sep}{value}", flags)
        for number, text in pages:
            m = rx.search(text)
            if m:
                return m, number
    return None


def _found(m: re.Match, number: int, value, confidence: float, group: int = 1) -> Found:
    return Found(value=value, page=number, start=m.start(group), end=m.end(group), quote=m.group(group).strip(),
                 confidence=confidence)


def parse_amount(raw: str) -> tuple[float | None, str | None]:
    currency = None
    if re.search(r"₹|Rs|INR", raw, re.I):
        currency = "INR"
    elif re.search(r"US\$|USD|\$", raw):
        currency = "USD"
    elif re.search(r"€|EUR", raw):
        currency = "EUR"
    elif re.search(r"£|GBP", raw):
        currency = "GBP"
    number = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    try:
        return (round(float(number.group(0).replace(",", "")), 2) if number else None), currency
    except ValueError:
        return None, currency


def _date_value(raw: str) -> str | None:
    for rx, style in DATE_RES:
        m = rx.search(raw)
        if m:
            return normalize_date(m, style)
    return None


def _labelled_date(pages, labels) -> Found | None:
    hit = _labelled(pages, labels, _DATE_ANY)
    if hit:
        value = _date_value(hit[0].group(1))
        if value:
            return _found(hit[0], hit[1], value, 0.9)
    return None


def _labelled_amount(pages, labels) -> Found | None:
    hit = _labelled(pages, labels, _AMOUNT)
    if hit:
        amount, _cur = parse_amount(hit[0].group(1))
        if amount is not None:
            return _found(hit[0], hit[1], amount, 0.9)
    return None


def _labelled_text(pages, labels, value=r"([^\n,]{2,120})", confidence=0.85, sep=r"\s*[:#\-]\s*") -> Found | None:
    hit = _labelled(pages, labels, value, sep=sep)
    if hit:
        text = re.sub(r"\s+", " ", hit[0].group(1)).strip(" .:-")
        if text:
            return _found(hit[0], hit[1], text, confidence)
    return None


def _first(pages, rx: re.Pattern, normalize: Callable[[str], object] = str, confidence: float = 0.8) -> Found | None:
    for number, text in pages:
        m = rx.search(text)
        if m:
            return Found(normalize(m.group(0)), number, m.start(), m.end(), m.group(0), confidence)
    return None


def _first_date(pages) -> Found | None:
    for number, text in pages:
        best = None
        for rx, style in DATE_RES:
            for m in rx.finditer(text):
                iso = normalize_date(m, style)
                if iso and (best is None or m.start() < best[0].start()):
                    best = (m, iso)
        if best:
            return Found(best[1], number, best[0].start(), best[0].end(), best[0].group(0), 0.6)
    return None


def _first_phone(pages) -> Found | None:
    for number, text in pages:
        for rx in PHONE_RES:
            m = rx.search(text)
            if m and 10 <= len(re.sub(r"\D", "", m.group(0))) <= 15:
                return Found(m.group(0).strip(), number, m.start(), m.end(), m.group(0), 0.8)
    return None


def _currency(pages, *amount_fields: Found | None) -> Found | None:
    for f in amount_fields:
        if f is not None:
            _amt, cur = parse_amount(f.quote)
            if cur:
                return Found(cur, f.page, f.start, f.end, f.quote, 0.8)
    rx = re.compile(_CUR)
    hit = _first(pages, rx)
    if hit:
        _amt, cur = parse_amount(hit.quote)
        if cur:
            return Found(cur, hit.page, hit.start, hit.end, hit.quote, 0.6)
    return None


def _first_line(pages) -> Found | None:
    for number, text in pages[:1]:
        offset = 0
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped:
                start = offset + line.index(stripped)
                return Found(stripped, number, start, start + len(stripped), stripped, 0.6)
            offset += len(line) + 1
    return None


def _invoice(pages) -> dict[str, Found | None]:
    subtotal = _labelled_amount(pages, [r"sub\s*-?\s*total", r"taxable\s+(?:value|amount)", r"net\s+amount"])
    tax = _labelled_amount(pages, [r"total\s+tax", r"tax\s+amount", r"(?:total\s+)?GST\s+amount", r"IGST", r"tax"])
    total = _labelled_amount(pages, [r"grand\s+total", r"amount\s+due", r"total\s+amount", r"balance\s+due",
                                     r"total\s+due", r"(?<![a-z])(?<!sub)(?<!sub-)(?<!sub )total"])
    buyer = _labelled_text(pages, [r"bill(?:ed)?\s+to", r"buyer", r"sold\s+to", r"customer(?:\s+name)?"])
    seller = _labelled_text(pages, [r"(?:from|seller|supplier|vendor|billed\s+by|sold\s+by)"])
    if seller is None:  # the first company name that is not the billed party
        for number, text in pages:
            for m in ORG_RE.finditer(text):
                value = re.sub(r"\s+", " ", m.group(1)).strip()
                if buyer is None or value.lower() not in buyer.value.lower():
                    seller = Found(value, number, m.start(1), m.end(1), m.group(1), 0.5)
                    break
            if seller:
                break
    return {
        "invoice_number": _labelled_text(pages, [r"(?:tax\s+)?invoice\s*(?:no\.?|number|num|#)", r"inv\s*(?:no\.?|#)",
                                                 r"bill\s*(?:no\.?|number|#)"], _ID, 0.9),
        "invoice_date": _labelled_date(pages, [r"(?:invoice|bill|issue)\s+date", r"dated", r"(?<!due )date"]),
        "due_date": _labelled_date(pages, [r"due\s+(?:date|on|by)", r"payment\s+due"]),
        "seller_name": seller,
        "buyer_name": buyer,
        "gstin": _first(pages, GSTIN_RE, str, 0.95),
        "subtotal": subtotal,
        "tax_amount": tax,
        "total_amount": total,
        "currency": _currency(pages, total, subtotal, tax),
        "email": _first(pages, EMAIL_RE, str.lower, 0.9),
        "phone": _first_phone(pages),
    }


def _receipt(pages) -> dict[str, Found | None]:
    total = _labelled_amount(pages, [r"grand\s+total", r"amount\s+paid", r"total\s+paid", r"net\s+payable",
                                     r"(?<![a-z])(?<!sub)(?<!sub-)(?<!sub )total"])
    tax = _labelled_amount(pages, [r"total\s+tax", r"tax", r"GST", r"VAT"])
    method = None
    for key, pattern in PAYMENT_METHODS:
        hit = _first(pages, re.compile(pattern, re.I), lambda _s, k=key: k, 0.8)
        if hit:
            method = hit
            break
    merchant = _labelled_text(pages, [r"(?:merchant|store|shop|sold\s+by)"])
    return {
        "receipt_number": _labelled_text(pages, [r"receipt\s*(?:no\.?|number|#)", r"(?:txn|transaction)\s*(?:id|no\.?|#)",
                                                 r"bill\s*(?:no\.?|#)", r"order\s*(?:id|no\.?|#)"], _ID, 0.9),
        "date": _labelled_date(pages, [r"(?:receipt|bill|txn|transaction|purchase)?\s*date"]) or _first_date(pages),
        "merchant": merchant or _first(pages, ORG_RE, lambda s: re.sub(r"\s+", " ", s).strip(), 0.6)
        or _first_line(pages),
        "tax_amount": tax,
        "total_amount": total,
        "payment_method": method,
        "currency": _currency(pages, total, tax),
    }


def _section_items(pages, heading: str, max_items: int = 40) -> Found | None:
    rx = re.compile(rf"(?im)^\s*(?:{heading})\s*[:\-]?\s*(.*)$")
    for number, text in pages:
        m = rx.search(text)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline:
            block, start = inline, m.start(1)
        else:  # the items are on the following lines, until a blank line or the next heading
            rest = text[m.end():]
            lines = []
            for line in rest.split("\n")[1:]:
                if not line.strip() or re.match(r"^\s*[A-Z][A-Za-z ]{2,30}:?\s*$", line) and lines:
                    break
                lines.append(line)
            block, start = "\n".join(lines), m.end() + 1
        items = [re.sub(r"^[\s•\-*·]+", "", p).strip() for p in re.split(r"[,;|\n•·]", block)]
        items = list(dict.fromkeys(i for i in items if 1 < len(i) <= 60))[:max_items]
        if items:
            return Found(items, number, start, start + len(block), block[:300], 0.8)
    return None


def _resume(pages) -> dict[str, Found | None]:
    name = None
    first = _first_line(pages)
    if first and re.fullmatch(r"[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3}", first.value or ""):
        name = Found(first.value, first.page, first.start, first.end, first.quote, 0.7)
    labelled_name = _labelled_text(pages, [r"(?<![a-z])name"], r"([A-Z][A-Za-z.'\- ]{2,60})", 0.9)
    years = _labelled(pages, [r"(?:total\s+)?experience"], r"(\d{1,2})\+?\s*(?:years|yrs)")
    if years is None:
        rx = re.compile(r"(\d{1,2})\+?\s*(?:years|yrs)(?:\s+of)?\s+(?:\w+\s+){0,3}experience", re.I)
        years = next(((m, n) for n, t in pages for m in [rx.search(t)] if m), None)
    education = []
    edu_hit = None
    for number, text in pages:
        for m in DEGREE_RE.finditer(text):
            education.append(re.sub(r"\s+", " ", m.group(0)).strip(" ,.;"))
            edu_hit = edu_hit or Found(None, number, m.start(), m.end(), m.group(0), 0.8)
    edu = None
    if education:
        edu = Found(list(dict.fromkeys(education))[:10], edu_hit.page, edu_hit.start, edu_hit.end, edu_hit.quote, 0.8)
    return {
        "name": labelled_name or name,
        "email": _first(pages, EMAIL_RE, str.lower, 0.95),
        "phone": _first_phone(pages),
        "current_title": _labelled_text(pages, [r"(?:current\s+)?(?:title|designation|role|position)"]),
        "years_experience": _found(years[0], years[1], int(years[0].group(1)), 0.8) if years else None,
        "skills": _section_items(pages, r"(?:technical\s+|key\s+|core\s+)?skills"),
        "education": edu,
        "linkedin": _first(pages, LINKEDIN_RE, str, 0.9),
    }


_EXTRACTORS = {"invoice": _invoice, "receipt": _receipt, "resume": _resume}


def extract_fields(schema_key: str, pages: list[tuple[int, str]]) -> Extraction:
    """pages: [(page_number, text)]. Returns values, citations and confidences for every schema field."""
    if schema_key not in SCHEMAS:
        raise KeyError(schema_key)
    found = _EXTRACTORS[schema_key](pages)
    out = Extraction(schema_key=schema_key)
    for name in field_names(schema_key):
        hit = found.get(name)
        if hit is None or hit.value in (None, "", []):
            out.result[name], out.citations[name], out.confidence[name] = None, None, None
        else:
            out.result[name] = hit.value
            out.citations[name] = [hit.citation()]
            out.confidence[name] = hit.confidence
    return out
