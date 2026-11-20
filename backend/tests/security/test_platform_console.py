"""Platform super-admin console (CONTRACT "W4 additions": Platform routes; API_SPECIFICATION Phase 11).

  * every `/api/v1/platform/*` route answers 404 to everyone who is not `User.is_platform_admin` (org owners,
    members, outsiders; every method), and never 200 to anonymous callers;
  * platform payloads carry counts and status only: after org A ingested the synthetic sales.csv and stored a
    dashboard note, no platform response contains any data VALUE from that CSV (customer names, order ids) nor
    the note;
  * suspend -> every member of the org gets 403 `org_suspended` on tenant routes (session and API key), other orgs
    are unaffected; reactivate restores access; both are audited in an append-only PlatformAuditEvent.

Skips cleanly until `apps.platform_admin` routes exist.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from testing.client import api_client_for
from testing.flows import upload_and_ingest
from testing.routes import UNSAFE_METHODS, platform_routes

ROUTES = [r for r in platform_routes() if r.methods]
REPO = Path(__file__).resolve().parents[3]
SALES = REPO / "testdata" / "generated" / "sales.csv"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not ROUTES, reason="/api/v1/platform/* not routed yet (Backend W4 apps.platform_admin)"),
]


def _platform_admin(two_orgs):
    from testing.factories import make_user

    user = make_user("qa-platform-admin@example.test")
    user.is_platform_admin = True
    user.save(update_fields=["is_platform_admin"])
    return user


def _values(route, two_orgs) -> dict[str, str]:
    a = two_orgs.a
    out = {}
    for kwarg in route.kwargs:
        if kwarg in ("pk", "id", "org_id", "org"):
            out[kwarg] = str(a.id)
        elif kwarg in ("model", "model_name"):
            out[kwarg] = "gemini-2.5-flash"
    return out


def _call(client, method: str, path: str):
    if method in UNSAFE_METHODS:
        return getattr(client, method)(path, {}, format="json")
    return client.get(path)


@pytest.mark.parametrize("route", ROUTES, ids=[r.id for r in ROUTES])
def test_non_platform_users_get_404_on_every_platform_route(two_orgs, route):
    a = two_orgs.a
    path = route.build(**_values(route, two_orgs))
    for user in (a.owner, a.user("admin"), a.analyst, a.viewer, two_orgs.outsider):
        client = api_client_for(user)
        for method in route.methods:
            resp = _call(client, method, path)
            assert resp.status_code == 404, f"{user.email} {method.upper()} {path} -> {resp.status_code}"
    anon = api_client_for(None)
    for method in route.methods:
        resp = _call(anon, method, path)
        assert resp.status_code in (401, 403, 404), f"anonymous {method.upper()} {path} -> {resp.status_code}"
    # an API key (org-bound, any scopes) is never a platform credential
    from testing.ml import api_key_for

    key = APIClient()
    key.credentials(HTTP_AUTHORIZATION=f"Bearer {api_key_for(a, a.owner, ['datasets.view'])}")
    assert key.get(path).status_code in (401, 403, 404)
    from apps.orgs.models import Organization

    assert Organization.objects.get(id=a.id).status == "active", "a refused call must not change the org"


def _sales_canaries() -> list[str]:
    with SALES.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    names = sorted({r["customer_name"] for r in rows if r.get("customer_name")})
    order_ids = sorted({r["order_id"] for r in rows if r.get("order_id")})
    return names[:200:7] + order_ids[:200:7]


def test_platform_payloads_contain_no_tenant_data_rows(two_orgs, memory_storage, fake_redis, fake_embedder,
                                                       django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner = api_client_for(a.owner)
    dataset = upload_and_ingest(owner, a.slug, "sales.csv", SALES.read_bytes(), django_capture_on_commit_callbacks,
                                name="Sales")
    note = "qa-platform-note-7f3a"
    board = owner.post(f"/api/v1/orgs/{a.slug}/dashboards", {"title": "Board"}, format="json")
    if board.status_code == 201:
        owner.post(f"/api/v1/orgs/{a.slug}/dashboards/{board.json()['id']}/widgets",
                   {"title": "n", "kind": "text", "text": note, "layout": {"x": 0, "y": 0, "w": 2, "h": 2}},
                   format="json")
    admin = api_client_for(_platform_admin(two_orgs))
    canaries = [*_sales_canaries(), note]
    probed = 0
    for route in ROUTES:
        if "get" not in route.methods:
            continue
        path = route.build(**_values(route, two_orgs))
        resp = admin.get(path)
        assert resp.status_code in (200, 404), f"GET {path} -> {resp.status_code}: {resp.content[:200]}"
        if resp.status_code != 200 or getattr(resp, "streaming", False):
            continue
        probed += 1
        text = resp.content.decode("utf-8", "replace")
        leaked = [c for c in canaries if c in text]
        assert leaked == [], f"GET {path} returned tenant data values: {leaked[:5]}"
    assert probed, "the platform admin could GET no platform route"
    orgs = admin.get("/api/v1/platform/orgs")
    if orgs.status_code == 200:
        body = json.dumps(orgs.json())
        assert str(a.id) in body, "the org list must list org A (counts and status)"
        assert dataset["id"] not in body, "the org list must not enumerate tenant resources"


def test_suspended_org_gets_403_org_suspended_and_reactivation_restores_access(two_orgs):
    from testing.ml import api_key_for

    a, b = two_orgs
    base_a, base_b = f"/api/v1/orgs/{a.slug}", f"/api/v1/orgs/{b.slug}"
    admin = api_client_for(_platform_admin(two_orgs))
    token = api_key_for(a, a.owner, ["datasets.view"])
    key = APIClient()
    key.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert api_client_for(a.owner).get(f"{base_a}/datasets").status_code == 200
    assert key.get(f"{base_a}/datasets").status_code == 200

    resp = admin.post(f"/api/v1/platform/orgs/{a.id}/suspend", {"reason": "qa"}, format="json")
    assert resp.status_code == 200, resp.content[:300]
    for user in (a.owner, a.analyst, a.viewer):
        for suffix in ("datasets", "jobs", "dashboards"):
            r = api_client_for(user).get(f"{base_a}/{suffix}")
            assert r.status_code == 403, f"{user.email} {suffix}: {r.status_code}"
            assert r.json().get("code") == "org_suspended", r.content[:200]
    r = api_client_for(a.owner).post(f"{base_a}/dashboards", {"title": "while suspended"}, format="json")
    assert r.status_code == 403 and r.json().get("code") == "org_suspended"
    r = key.get(f"{base_a}/datasets")
    assert r.status_code == 403 and r.json().get("code") == "org_suspended", "API keys of a suspended org too"
    assert api_client_for(b.owner).get(f"{base_b}/datasets").status_code == 200, "other orgs are unaffected"
    assert api_client_for(b.owner).get(f"{base_a}/datasets").status_code == 404, "non-members still get 404"

    resp = admin.post(f"/api/v1/platform/orgs/{a.id}/reactivate", {}, format="json")
    assert resp.status_code == 200, resp.content[:300]
    for user in (a.owner, a.viewer):
        assert api_client_for(user).get(f"{base_a}/datasets").status_code == 200
    assert key.get(f"{base_a}/datasets").status_code == 200

    from django.apps import apps as django_apps

    model = next((m for m in django_apps.get_app_config("platform_admin").get_models()
                  if m.__name__ == "PlatformAuditEvent"), None)
    assert model is not None, "suspend/reactivate must be audited in PlatformAuditEvent"
    events = list(model.objects.using("ops").all()) if "ops" in _db_aliases() else list(model.objects.all())
    blob = json.dumps([{f.name: str(getattr(e, f.name)) for f in model._meta.concrete_fields} for e in events])
    assert "suspend" in blob and "reactivate" in blob and str(a.id) in blob, blob[:400]


def _db_aliases() -> set[str]:
    from django.conf import settings

    return set(settings.DATABASES)


def test_platform_audit_event_is_append_only():
    from django.apps import apps as django_apps
    from django.db import connection

    model = next((m for m in django_apps.get_app_config("platform_admin").get_models()
                  if m.__name__ == "PlatformAuditEvent"), None)
    if model is None:
        pytest.skip("PlatformAuditEvent not built yet")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE t.tgrelid = %s::regclass AND t.tgname = 'append_only'",
            [model._meta.db_table],
        )
        row = cur.fetchone()
    assert row and row[0] == "nexora_forbid_mutation", f"{model._meta.db_table} must be MakeAppendOnly (strict)"
