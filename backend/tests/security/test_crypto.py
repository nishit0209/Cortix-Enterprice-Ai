"""Envelope encryption (apps.core.crypto): round trip, AAD binding, tamper detection, KEK validation."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.exceptions import InvalidTag

from apps.core.crypto import KEK_ID, decrypt_secret, encrypt_secret

AAD = "org:018f0000-0000-7000-8000-00000000000a:credential:018f0000-0000-7000-8000-0000000000c1"
SECRET = "synthetic-db-password-ÄÖÜ-✓-123"


def _flip(b64: str, index: int = 0) -> str:
    raw = bytearray(base64.b64decode(b64))
    raw[index % len(raw)] ^= 0x01
    return base64.b64encode(bytes(raw)).decode()


def test_round_trip_and_blob_shape():
    blob = encrypt_secret(SECRET, aad=AAD)
    assert set(blob) == {"v", "kek_id", "ct", "nonce", "wdek", "wnonce"}
    assert blob["v"] == 1 and blob["kek_id"] == KEK_ID
    assert len(base64.b64decode(blob["nonce"])) == 12 and len(base64.b64decode(blob["wnonce"])) == 12
    assert decrypt_secret(blob, aad=AAD) == SECRET
    json.dumps(blob)  # JSON-serialisable for storage


def test_plaintext_never_appears_in_the_blob():
    blob = encrypt_secret(SECRET, aad=AAD)
    dump = json.dumps(blob)
    assert SECRET not in dump
    assert base64.b64encode(SECRET.encode()).decode() not in dump


def test_encryption_is_randomised():
    b1, b2 = encrypt_secret(SECRET, aad=AAD), encrypt_secret(SECRET, aad=AAD)
    assert b1["ct"] != b2["ct"] and b1["wdek"] != b2["wdek"] and b1["nonce"] != b2["nonce"]


def test_aad_binds_the_secret_to_its_org_and_record():
    blob = encrypt_secret(SECRET, aad=AAD)
    other_org = AAD.replace("00000000000a", "00000000000b")
    with pytest.raises(InvalidTag):
        decrypt_secret(blob, aad=other_org)
    with pytest.raises(InvalidTag):
        decrypt_secret(blob, aad="")


@pytest.mark.parametrize("field", ["ct", "nonce", "wdek", "wnonce"])
@pytest.mark.parametrize("index", [0, 7, -1])
def test_any_tampering_is_detected(field, index):
    blob = encrypt_secret(SECRET, aad=AAD)
    blob[field] = _flip(blob[field], index)
    with pytest.raises(InvalidTag):
        decrypt_secret(blob, aad=AAD)


def test_swapping_wrapped_keys_between_records_fails():
    b1 = encrypt_secret("first", aad=AAD)
    b2 = encrypt_secret("second", aad=AAD)
    mixed = {**b1, "wdek": b2["wdek"], "wnonce": b2["wnonce"]}
    with pytest.raises(InvalidTag):
        decrypt_secret(mixed, aad=AAD)


def test_wrong_kek_cannot_decrypt(settings):
    blob = encrypt_secret(SECRET, aad=AAD)
    settings.NEXORA_KEK = base64.b64encode(b"\x01" * 32).decode()
    with pytest.raises(InvalidTag):
        decrypt_secret(blob, aad=AAD)


@pytest.mark.parametrize("bad", [base64.b64encode(b"short").decode(), base64.b64encode(b"x" * 33).decode()])
def test_kek_must_be_exactly_32_bytes(settings, bad):
    settings.NEXORA_KEK = bad
    with pytest.raises(ValueError):
        encrypt_secret(SECRET, aad=AAD)


def test_empty_and_large_secrets_round_trip():
    for value in ("", "x" * 100_000):
        assert decrypt_secret(encrypt_secret(value, aad=AAD), aad=AAD) == value
