"""Data-source credentials are write-only (CONTRACT W2 "password write-only"; SECURITY_ARCHITECTURE §6).

A canary password given to POST /data-sources must never appear in: any API response (create, detail, list,
patch, test job, audit events), any log record at any level, any Redis job event, or ANY tenant table in
plaintext (the credential row is envelope-encrypted). Not even when the driver error echoes connection params.
Skips while the data-sources API is not merged.
"""

from __future__ import annotations

import json
import logging

import pytest

from testing.client import api_client_for, requires_tenant_route, route_exists
from testing.db import db_tenant, tenant_tables
from testing.flows import items, job_outcome
from testing.network import ConnectSpy, Resolver

pytestmark = [pytest.mark.django_db, requires_tenant_route("data-sources", "Data Engineering")]


@pytest.fixture
def source_api(
    two_orgs, settings, monkeypatch, fake_redis, canaries, caplog, django_capture_on_commit_callbacks
):
    a = two_orgs.a
    base = f"/api/v1/orgs/{a.slug}/data-sources"
    if not route_exists(base):
        pytest.skip("data-sources API not implemented yet (Data Engineering, wave 2)")
    settings.ALLOW_PRIVATE_DB_HOSTS = True  # reach the (spied) connect step instead of the SSRF refusal
    caplog.set_level(logging.DEBUG)
    # a driver error that echoes every connection parameter, password included (some libpq errors do)
    spy = ConnectSpy(error_message="could not connect: host={host} user={user} password={password}")
    spy.install(monkeypatch)
    # resolve the synthetic name (private, allowed above) so the worker reaches the spied connect step
    Resolver(answers={"db.example.test": ["10.20.30.40"]}).install(monkeypatch)
    client = api_client_for(a.owner)
    responses: list[str] = []

    def call(method: str, path: str, body=None):
        fn = getattr(client, method)
        if method in ("post", "patch", "put"):
            with django_capture_on_commit_callbacks(execute=True):
                resp = fn(path, body or {}, format="json")
        else:
            resp = fn(path)
        responses.append(resp.content.decode("utf-8", errors="replace"))
        return resp

    call.base, call.world, call.responses, call.spy = base, a, responses, spy
    return call


def _create(source_api, canaries):
    body = {
        "name": "Synthetic warehouse replica",
        "type": "postgresql",
        "config": {
            "host": "db.example.test",
            "port": 5999,
            "dbname": ConnectSpy.PROBE_DB,
            "sslmode": "require",
            "schema": "public",
        },
        "username": "nexora_ro",
        "password": canaries.db_password,
    }
    resp = source_api("post", source_api.base, body)
    assert resp.status_code in (200, 201), resp.content[:300]
    return resp.json()


def _all_text(source_api, caplog, fake_redis) -> str:
    redis_events = repr(fake_redis.streams)  # stream keys and fields are bytes
    return "\n".join([*source_api.responses, caplog.text, redis_events])


@pytest.mark.ac("P3-AC2")
def test_password_is_never_returned_logged_or_published(source_api, canaries, caplog, fake_redis):
    created = _create(source_api, canaries)
    source_id = created["id"]
    assert "password" not in created or created["password"] in (None, "", "********")
    source_api("get", f"{source_api.base}/{source_id}")
    listed = source_api("get", source_api.base)
    assert source_id in json.dumps(items(listed))
    source_api("patch", f"{source_api.base}/{source_id}", {"name": "Renamed replica"})
    tested = source_api("post", f"{source_api.base}/{source_id}/test", {})
    if tested.status_code == 202:
        job = job_outcome(
            api_client_for(source_api.world.owner), source_api.world.slug, tested.json()["job_id"]
        )
        source_api.responses.append(json.dumps(job))
    source_api("get", f"/api/v1/orgs/{source_api.world.slug}/audit-events")
    assert source_api.spy.attempts, "the test never reached the connection step (spy saw nothing)"
    assert source_api.spy.attempts[0]["password"] == canaries.db_password  # the worker DID decrypt it...
    leaks = canaries.find_in(_all_text(source_api, caplog, fake_redis))
    assert leaks == [], f"credential canary leaked: {leaks}"  # ...but it never surfaced anywhere


@pytest.mark.ac("P3-AC2")
def test_password_is_not_stored_in_plaintext_in_any_tenant_table(source_api, canaries):
    from django.db import connection

    _create(source_api, canaries)
    found = []
    with db_tenant(source_api.world.id), connection.cursor() as cur:
        for table in tenant_tables():
            cur.execute(
                f'SELECT count(*) FROM "{table}" t WHERE row_to_json(t)::text LIKE %s',  # noqa: S608
                [f"%{canaries.db_password}%"],
            )
            if cur.fetchone()[0]:
                found.append(table)
    assert found == [], f"plaintext credential stored in {found}"
