"""Secrets hygiene (TESTING_STRATEGY Â§4.4, P2-AC5): canary secrets never reach logs, responses or audit rows.

Each test plants unique canaries (testing.security.Canaries), runs a flow with ALL loggers captured at DEBUG,
then scans the captured log text, HTTP bodies and stored rows.
"""

from __future__ import annotations

import logging

import pytest
from django.db import OperationalError, connection

from apps.core import audit, jobs
from apps.core.crypto import decrypt_secret, encrypt_secret
from testing.client import api_client_for, route_exists
from testing.db import clear_db_tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def capture_all(caplog):
    caplog.set_level(logging.DEBUG)
    for name in ("django", "django.request", "django.db.backends", "celery", "apps", "nexora"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    return caplog


def _assert_clean(canaries, *texts: str) -> None:
    for text in texts:
        assert canaries.find_in(text) == [], f"canary secret leaked: {canaries.find_in(text)}"


def test_user_password_is_only_stored_hashed(canaries, capture_all, make_user):
    user = make_user("canary-user@example.test", password=canaries.user_password)
    with connection.cursor() as cur:
        cur.execute("SELECT password FROM accounts_user WHERE id = %s", [str(user.pk)])
        stored = cur.fetchone()[0]
    assert user.check_password(canaries.user_password)
    _assert_clean(canaries, stored, capture_all.text)


def test_audit_rows_and_logs_never_contain_secret_values(canaries, capture_all, two_orgs, tenant):
    with tenant(two_orgs.a.org):
        ev = audit.record(
            "qa.secrets",
            metadata={"password": canaries.db_password, "gemini_api_key": canaries.gemini_key,
                      "nested": {"api_key": canaries.api_key, "session_cookie": canaries.session_id}},
            changes={"token": [None, canaries.api_key], "credential": canaries.kek_plain},
        )
    clear_db_tenant()
    with tenant(two_orgs.a.org), connection.cursor() as cur:
        cur.execute("SELECT metadata::text, coalesce(changes::text, '') FROM core_auditevent WHERE id = %s", [str(ev.id)])
        metadata, changes = cur.fetchone()
    _assert_clean(canaries, metadata, changes, capture_all.text)


def test_encrypted_credentials_never_log_plaintext(canaries, capture_all):
    blob = encrypt_secret(canaries.kek_plain, aad="org:x:credential:y")
    assert decrypt_secret(blob, aad="org:x:credential:y") == canaries.kek_plain
    _assert_clean(canaries, str(blob), capture_all.text)


def test_job_flows_do_not_log_settings_secrets(canaries, capture_all, settings, two_orgs, tenant, fake_redis):
    settings.GEMINI_API_KEY = canaries.gemini_key
    settings.API_KEY_PEPPER = canaries.api_key
    with tenant(two_orgs.a.org):
        job = jobs.create_job("qa.secrets", user_id=two_orgs.a.owner.pk)
        jobs.update(job.id, status="running", progress=50, message="working")
        jobs.finish(job.id, result={"ok": True})
    stream = str(fake_redis.streams)
    _assert_clean(canaries, stream, capture_all.text)


def test_readiness_errors_expose_only_exception_types(canaries, capture_all, monkeypatch, client):
    """/readyz is unauthenticated: dependency errors must not echo DSNs, passwords or keys."""
    from apps.core import storage, views

    class _BrokenConn:
        def cursor(self):
            raise OperationalError(f'connection failed: password authentication failed "{canaries.db_password}"')

    class _BrokenRedis:
        def ping(self):
            raise ConnectionError(f"redis://:{canaries.db_password}@redis:6379/0 unreachable")

    class _BrokenS3:
        def head_bucket(self, **_kw):
            raise RuntimeError(f"InvalidAccessKeyId {canaries.api_key}")

    import redis

    monkeypatch.setattr(views, "connection", _BrokenConn())
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, *a, **k: _BrokenRedis()))
    monkeypatch.setattr(storage, "client", lambda: _BrokenS3())
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"] == {
        "database": "error: OperationalError", "redis": "error: ConnectionError", "object_storage": "error: RuntimeError"
    }
    _assert_clean(canaries, resp.content.decode(), capture_all.text)


def test_login_failure_does_not_echo_or_log_the_password(canaries, capture_all, make_user):
    if not route_exists("/api/v1/auth/login"):
        pytest.skip("POST /api/v1/auth/login not implemented yet (Backend agent, wave 1)")
    make_user("canary-login@example.test")
    resp = api_client_for(None).post(
        "/api/v1/auth/login", {"email": "canary-login@example.test", "password": canaries.user_password}, format="json"
    )
    assert resp.status_code in (400, 401, 403)
    _assert_clean(canaries, resp.content.decode(), capture_all.text)


def test_signup_does_not_echo_or_log_the_password(canaries, capture_all):
    if not route_exists("/api/v1/auth/signup"):
        pytest.skip("POST /api/v1/auth/signup not implemented yet (Backend agent, wave 1)")
    resp = api_client_for(None).post(
        "/api/v1/auth/signup",
        {"email": "canary-signup@example.test", "password": canaries.user_password, "full_name": "Canary",
         "org_name": "Canary Org"},
        format="json",
    )
    assert resp.status_code in (201, 400)
    _assert_clean(canaries, resp.content.decode(), capture_all.text)


def test_api_key_secret_is_returned_once_and_never_logged(canaries, capture_all, two_orgs):
    a = two_orgs.a
    url = f"/api/v1/orgs/{a.slug}/api-keys"
    if not route_exists(url):
        pytest.skip("API keys not implemented yet (Backend agent, wave 1)")
    client = api_client_for(a.owner)
    created = client.post(url, {"name": "canary", "scopes": []}, format="json")
    assert created.status_code == 201
    secret = created.json()["secret"]
    listing = client.get(url).content.decode()
    assert secret not in listing
    assert secret not in capture_all.text
    with a.tenant(), connection.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE %s",
            ["%api%key%"],
        )
        tables = [r[0] for r in cur.fetchall()]
        assert tables, "no API key table found"
        stored = ""
        for table in tables:
            cur.execute(f'SELECT coalesce(string_agg(row_to_json(k)::text, \' \'), \'\') FROM "{table}" k')
            stored += cur.fetchone()[0]
    assert secret not in stored, "API key secrets must be stored hashed"
