"""Envelope encryption for secrets (customer DB passwords, API tokens).

    blob = encrypt_secret("s3cr3t", aad=f"org:{org_id}:credential:{cred_id}")
    plain = decrypt_secret(blob, aad=...)

Per-secret random 256-bit DEK (AES-256-GCM), DEK wrapped with the KEK from NEXORA_KEK (base64 32 bytes).
Stored blob is a JSON-serialisable dict; never log or return it via APIs.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

KEK_ID = "env-v1"


def _kek() -> bytes:
    key = base64.b64decode(settings.NEXORA_KEK)
    if len(key) != 32:
        raise ValueError("NEXORA_KEK must be base64 of exactly 32 bytes")
    return key


def encrypt_secret(plaintext: str, *, aad: str) -> dict:
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode(), aad.encode())
    wrap_nonce = os.urandom(12)
    wrapped = AESGCM(_kek()).encrypt(wrap_nonce, dek, aad.encode())
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return {"v": 1, "kek_id": KEK_ID, "ct": b64(ct), "nonce": b64(nonce), "wdek": b64(wrapped), "wnonce": b64(wrap_nonce)}


def decrypt_secret(blob: dict, *, aad: str) -> str:
    d = lambda k: base64.b64decode(blob[k])  # noqa: E731
    dek = AESGCM(_kek()).decrypt(d("wnonce"), d("wdek"), aad.encode())
    return AESGCM(dek).decrypt(d("nonce"), d("ct"), aad.encode()).decode()
