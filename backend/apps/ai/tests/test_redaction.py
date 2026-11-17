"""PII redactor applied to every prompt (synthetic values only)."""

from __future__ import annotations

import pytest

from apps.ai.redaction import luhn_valid, redact


@pytest.mark.parametrize(("text", "kind"), [
    ("contact asha.verma@example.test now", "email"),
    ("card 4111 1111 1111 1111 on file", "card"),
    ("card 4111-1111-1111-1111", "card"),
    ("aadhaar 2345 6789 0123 given", "aadhaar"),
    ("PAN ABCPE1234F belongs to", "pan"),
    ("call +91 98765 43210 today", "phone"),
    ("call 9876543210 today", "phone"),
    ("office (022) 2345-6789", "phone"),
])
def test_masks_pii(text, kind):
    res = redact(text)
    assert res.counts.get(kind) == 1, res
    assert f"[{kind.upper()}]" in res.text


@pytest.mark.parametrize("text", [
    "SELECT region, SUM(amount) FROM sales WHERE order_date >= '2026-10-01' LIMIT 1000",
    "top 5 regions by revenue in 2025",
    "order 123456 was shipped",
    "ratio 0.93 and 1500000 rows",
    "4111 1111 1111 1112 is not a valid card",  # fails Luhn
])
def test_leaves_non_pii_alone(text):
    res = redact(text)
    assert res.total == 0, res
    assert res.text == text


def test_multiple_and_counts():
    res = redact("a@example.test, b@example.test and 4111111111111111")
    assert res.counts == {"email": 2, "card": 1}
    assert "example.test" not in res.text


def test_luhn():
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")


def test_empty():
    assert redact("").text == ""
