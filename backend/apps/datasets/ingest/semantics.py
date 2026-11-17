"""Semantic types (regex + heuristics) and PII tagging (column-name heuristics + regex hit rate).

Semantic types: id, email, phone, url, currency, percent, date, category, free_text, person_name,
geo_country, geo_city, boolean, number, json, text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl

from .geo import CITY_NAMES, COUNTRY_CODES, COUNTRY_NAMES
from .inference import TypePlan

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
URL_RE = re.compile(r"^(?:https?://|www\.)[^\s]+$", re.I)
PHONE_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
PHONE_IN_RE = re.compile(r"^(?:\+?91|0)?[6-9]\d{9}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
AADHAAR_SPACED_RE = re.compile(r"^[2-9]\d{3}[ -]\d{4}[ -]\d{4}$")
AADHAAR_RE = re.compile(r"^[2-9]\d{11}$")
SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$")
CARD_RE = re.compile(r"^(?:\d[ -]?){12,18}\d$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

PII_HIT_RATE = 0.5
SEMANTIC_HIT_RATE = 0.9
GEO_HIT_RATE = 0.8
DETECTION_SAMPLE = 2000


def _tokens(name: str) -> list[str]:
    return [t for t in name.lower().split("_") if t]


def _normalize_phone(value: str) -> str:
    return re.sub(r"[\s\-().]", "", value)


def _luhn_ok(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _rate(values: Sequence[str], pred) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if pred(v)) / len(values)


# ---------------------------------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------------------------------
_PII_NAME_RULES: list[tuple[str, set[str] | None, set[str]]] = [
    # (pii_type, exact-name set, token set)
    ("ip_address", {"ip", "ip_address", "ipaddress", "client_ip", "ip_addr"}, set()),
    ("email", {"email", "e_mail", "email_address", "mail", "email_id"}, {"email"}),
    ("phone", {"phone", "mobile", "cell", "telephone", "tel", "whatsapp", "contact_no", "contact_number"},
     {"phone", "mobile", "telephone", "whatsapp", "msisdn"}),
    ("national_id", {"aadhaar", "aadhar", "pan", "pan_no", "pan_number", "ssn", "passport", "passport_no",
                     "voter_id", "national_id", "tax_id", "driving_licence", "driving_license"},
     {"aadhaar", "aadhar", "ssn", "passport"}),
    ("date_of_birth", {"dob", "date_of_birth", "birth_date", "birthdate", "birthday"}, {"dob", "birthdate", "birthday"}),
    ("address", {"address", "street", "address_line_1", "address_line_2", "pincode", "pin_code", "zip", "zipcode",
                 "zip_code", "postal_code", "postcode"}, {"address", "street", "pincode", "zipcode", "postcode"}),
    ("financial", {"card_number", "credit_card", "debit_card", "account_number", "account_no", "bank_account",
                   "iban", "upi_id", "cvv"}, {"iban", "cvv"}),
]
_PERSON_QUALIFIERS = {"first", "last", "full", "middle", "given", "sur", "father", "mother", "customer", "employee",
                      "person", "patient", "contact", "user", "owner", "member", "student", "guardian", "spouse"}


def pii_from_name(name: str) -> str | None:
    lowered = name.lower()
    toks = set(_tokens(lowered))
    for pii_type, exact, tokens in _PII_NAME_RULES:
        if lowered in (exact or set()) or toks & tokens:
            return pii_type
    if lowered in ("name", "surname", "fullname", "firstname", "lastname") or (
        "name" in toks and toks & _PERSON_QUALIFIERS
    ):
        return "person_name"
    return None


def pii_from_values(values: Sequence[str], name_hint: str | None = None) -> tuple[str | None, float]:
    """Regex hit-rate detection on sample values; returns (pii_type, hit rate)."""
    if not values:
        return None, 0.0
    checks = [
        ("email", lambda v: bool(EMAIL_RE.match(v))),
        ("national_id", lambda v: bool(PAN_RE.match(v) or AADHAAR_SPACED_RE.match(v) or SSN_RE.match(v))),
        ("financial", lambda v: bool(CARD_RE.match(v)) and _luhn_ok(v)),
        ("ip_address", lambda v: bool(IPV4_RE.match(v))),
        ("phone", lambda v: bool(PHONE_E164_RE.match(_normalize_phone(v)))
         or (bool(re.search(r"[\s\-+()]", v)) and bool(PHONE_IN_RE.match(_normalize_phone(v))))),
    ]
    if name_hint in ("phone", "national_id"):
        checks.append(("phone", lambda v: bool(PHONE_IN_RE.match(_normalize_phone(v)))))
        checks.append(("national_id", lambda v: bool(AADHAAR_RE.match(_normalize_phone(v)))))
    best, best_rate = None, 0.0
    for pii_type, pred in checks:
        rate = _rate(values, pred)
        if rate > best_rate:
            best, best_rate = pii_type, rate
    if best_rate >= PII_HIT_RATE:
        return best, best_rate
    return None, best_rate


@dataclass(frozen=True)
class PiiResult:
    is_pii: bool
    pii_type: str | None
    source: str | None  # "name" | "values" | "name+values"
    hit_rate: float = 0.0


def detect_pii(name: str, sample: Sequence[str]) -> PiiResult:
    values = [v for v in sample[:DETECTION_SAMPLE] if v]
    by_name = pii_from_name(name)
    by_values, rate = pii_from_values(values, name_hint=by_name)
    if by_name and by_values:
        return PiiResult(True, by_values if by_values != "phone" or by_name == "phone" else by_name, "name+values", rate)
    if by_values:
        return PiiResult(True, by_values, "values", rate)
    if by_name:
        return PiiResult(True, by_name, "name", rate)
    return PiiResult(False, None, None, rate)


# ---------------------------------------------------------------------------------------------------
# semantic types
# ---------------------------------------------------------------------------------------------------
_ID_NAME_RE = re.compile(r"(^id$|_id$|^id_|uuid|guid|_key$|^key$|_code$|^code$|_no$|_num$|_number$|^sku$|_sku$)")
_COUNTRY_NAME_RE = re.compile(r"(country|nation)")
_CITY_NAME_RE = re.compile(r"(city|town|district|location)")


def is_category(distinct: int, non_null: int) -> bool:
    if distinct == 0 or non_null == 0 or distinct > 1000:
        return False
    if distinct <= 0.05 * non_null:
        return True
    return non_null < 1000 and distinct < non_null and distinct <= max(2, non_null // 5)


def detect_semantic(
    name: str, plan: TypePlan, typed: pl.Series, sample: Sequence[str], *, pii_type: str | None = None
) -> str | None:
    pg = plan.pg_type
    values = [v for v in sample[:DETECTION_SAMPLE] if v]
    non_null = typed.len() - typed.null_count()
    distinct = typed.drop_nulls().n_unique() if non_null else 0
    lowered = name.lower()
    if pg == "jsonb":
        return "json"
    if pg in ("date", "timestamptz"):
        return "date"
    if pg == "boolean":
        return "boolean"
    if pg in ("numeric", "double precision", "bigint"):
        if plan.currency_ratio >= 0.5:
            return "currency"
        if plan.percent_ratio >= 0.5 or re.search(r"(pct|percent|percentage|_rate$)", lowered):
            return "percent" if plan.percent_ratio >= 0.5 or pg != "bigint" else "number"
        if pii_type == "phone":
            return "phone"
        if pg == "bigint" and non_null and distinct == non_null and (
            _ID_NAME_RE.search(lowered) or (non_null > 1 and typed.drop_nulls().is_sorted())
        ):
            return "id"
        if re.search(r"(price|amount|revenue|cost|salary|fee|total|balance|mrp|gmv|sales)", lowered) and pg != "bigint":
            return "currency"
        return "number"
    # text
    if values:
        if _rate(values, lambda v: bool(EMAIL_RE.match(v))) >= SEMANTIC_HIT_RATE:
            return "email"
        if _rate(values, lambda v: bool(URL_RE.match(v))) >= SEMANTIC_HIT_RATE:
            return "url"
        if pii_type == "phone" or _rate(
            values, lambda v: bool(PHONE_E164_RE.match(_normalize_phone(v)) or PHONE_IN_RE.match(_normalize_phone(v)))
        ) >= SEMANTIC_HIT_RATE and not _ID_NAME_RE.search(lowered):
            return "phone"
        if _rate(values, lambda v: bool(UUID_RE.match(v))) >= SEMANTIC_HIT_RATE:
            return "id"
        distinct_values = {v.strip().lower() for v in values}
        country_hits = sum(1 for v in distinct_values if v in COUNTRY_NAMES)
        if _COUNTRY_NAME_RE.search(lowered):
            country_hits = sum(1 for v in distinct_values if v in COUNTRY_NAMES or v in COUNTRY_CODES)
        if distinct_values and country_hits / len(distinct_values) >= GEO_HIT_RATE:
            return "geo_country"
        city_hits = sum(1 for v in distinct_values if v in CITY_NAMES)
        if distinct_values and city_hits / len(distinct_values) >= GEO_HIT_RATE and (
            _CITY_NAME_RE.search(lowered) or len(distinct_values) >= 3
        ):
            return "geo_city"
    if pii_type == "person_name":
        return "person_name"
    if non_null and distinct == non_null and _ID_NAME_RE.search(lowered):
        return "id"
    if is_category(distinct, non_null):
        return "category"
    if values:
        avg_len = sum(len(v) for v in values) / len(values)
        avg_words = sum(len(v.split()) for v in values) / len(values)
        if avg_len >= 40 or avg_words >= 5:
            return "free_text"
    if non_null and distinct >= 0.99 * non_null and _ID_NAME_RE.search(lowered):
        return "id"
    return "text"
