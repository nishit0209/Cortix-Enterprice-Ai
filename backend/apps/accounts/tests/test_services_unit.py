"""Unit tests for accounts services that need no database."""

import hashlib
import hmac

import pytest

from apps.accounts import services


def test_api_key_format_roundtrip():
    token = services.format_api_key("ab12cd34", "s" * 43)
    assert token.startswith("nxk_ab12cd34_")
    assert services.parse_api_key(token) == ("ab12cd34", "s" * 43)


def test_secret_may_contain_underscores():
    secret = "abc_def-" * 6
    assert services.parse_api_key(f"nxk_ab12cd34_{secret}") == ("ab12cd34", secret)


@pytest.mark.parametrize(
    "raw",
    ["", "nxk_", "nxk_short_" + "s" * 43, "abc_ab12cd34_" + "s" * 43, "nxk_AB12CD34_" + "s" * 43,
     "nxk_ab12cd34_tooshort", "nxk_ab-2cd34_" + "s" * 43],
)
def test_parse_rejects_malformed(raw):
    assert services.parse_api_key(raw) is None


def test_hash_is_hmac_sha256_with_pepper(settings):
    settings.API_KEY_PEPPER = "unit-test-pepper"
    expected = hmac.new(b"unit-test-pepper", b"secret-value", hashlib.sha256).digest()
    assert services.hash_api_secret("secret-value") == expected
    settings.API_KEY_PEPPER = "another-pepper"
    assert services.hash_api_secret("secret-value") != expected


def test_forbidden_scopes_are_never_allowed():
    assert "members.manage" not in services.API_KEY_ALLOWED_SCOPES
    assert "api_keys.manage" not in services.API_KEY_ALLOWED_SCOPES
    assert "org.ai_policy.consent" not in services.API_KEY_ALLOWED_SCOPES
    assert "governance.policy.edit" not in services.API_KEY_ALLOWED_SCOPES
    assert "ml.predict" in services.API_KEY_ALLOWED_SCOPES


def test_login_lockout_exponential_backoff():
    email, ip = "Lock.Me@Example.test", "203.0.113.7"
    for _ in range(services.LOGIN_FAILURE_THRESHOLD - 1):
        assert services.register_login_failure(email, ip) == 0
    assert services.login_lock_remaining(email, ip) == 0
    assert services.register_login_failure(email, ip) == 30
    assert 0 < services.login_lock_remaining("lock.me@example.test", ip) <= 30  # case-insensitive key
    assert services.register_login_failure(email, ip) == 60
    assert services.login_lock_remaining(email, "198.51.100.1") == 0  # other IP unaffected
    services.reset_login_failures(email, ip)
    assert services.login_lock_remaining(email, ip) == 0


def test_lock_is_capped():
    for _ in range(20):
        lock = services.register_login_failure("cap@example.test", "203.0.113.8")
    assert lock == services.LOGIN_MAX_LOCK_S
