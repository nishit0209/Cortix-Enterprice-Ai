"""Governance retention + usage (CONTRACT "W4 additions"; API_SPECIFICATION Phase 5 rows; Backend W4 mission B).

Retention
  * every append-only table is protected at the database level: its `append_only` trigger runs the STRICT
    `nexora_forbid_mutation` function (DELETE refused even for nexora_ops). The only exceptions are tables
    created with `ops_may_delete` (short-lived event streams such as core.JobEvent), listed explicitly here;
  * a retention run with policies for EVERY registered target, evaluated 30 days ahead, deletes no row of any strict
    append-only table;
  * a DRY RUN changes nothing at all (row counts of every tenant table are identical before and after);
  * the retention-policies API refuses an append-only target.
Usage
  * `GET /usage` and `GET /ai/usage` are tenant-scoped: org A's numbers count only A's activity, never B's, and a
    member of B gets 404 on A's usage.

DB-backed. Each block skips cleanly until its feature exists (RetentionPolicy model / registered
`governance.retention` job / usage routes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.db import connection

from testing.client import api_client_for, tenant_route_exists
from testing.flows import upload_and_ingest

pytestmark = pytest.mark.django_db

REPO = Path(__file__).resolve().parents[3]
SALES = REPO / "testdata" / "generated" / "sales.csv"
# append-only tables that may be purged by nexora_ops by design (MakeAppendOnly(ops_may_delete=True))
# (ML prediction logs and drift reports are high-volume telemetry, like job events: the app role still cannot change
# them; only nexora_ops may DELETE, never UPDATE)
OPS_PURGEABLE = {"core_jobevent", "ml_predictionlog", "ml_driftreport"}


def _append_only_tables() -> dict[str, str]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname, p.proname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_proc p ON p.oid = t.tgfoid WHERE t.tgname = 'append_only' AND NOT t.tgisinternal"
        )
        return dict(cur.fetchall())


def _strict_tables() -> list[str]:
    return sorted(t for t, fn in _append_only_tables().items() if fn == "nexora_forbid_mutation")


def _app_readable(tables: list[str]) -> list[str]:
    """The tables the app role can SELECT (platform tables are ops-only: retention, running as the app, cannot even
    read them, let alone delete)."""
    with connection.cursor() as cur:
        cur.execute("SELECT t FROM unnest(%s::text[]) t WHERE has_table_privilege(current_user, t, 'SELECT')",
                    [tables])
        return sorted(r[0] for r in cur.fetchall())


def _tenant_tables() -> list[str]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity"
        )
        return sorted(r[0] for r in cur.fetchall())


def _counts(world, tables) -> dict[str, int]:
    out = {}
    with world.tenant(), connection.cursor() as cur:
        for t in tables:
            cur.execute(f'SELECT count(*) FROM "{t}"')
            out[t] = cur.fetchone()[0]
    return out


# ------------------------------------------------------------------------------------------------ DB protection
def test_append_only_tables_use_the_strict_trigger_unless_listed_as_ops_purgeable():
    tables = _append_only_tables()
    assert tables, "no append-only table found (MakeAppendOnly not applied?)"
    loose = sorted(t for t, fn in tables.items() if fn != "nexora_forbid_mutation" and t not in OPS_PURGEABLE)
    assert loose == [], f"append-only tables a purge (retention) could delete from: {loose}"
    with connection.cursor() as cur:
        cur.execute("SELECT prosrc FROM pg_proc WHERE proname = 'nexora_forbid_mutation'")
        src = cur.fetchone()[0]
    assert "RETURN OLD" not in src.upper(), "the strict append-only function must never let a DELETE through"


# ------------------------------------------------------------------------------------------------ retention runs
def _retention_model():
    from django.apps import apps as django_apps

    try:
        return django_apps.get_model("governance", "RetentionPolicy")
    except LookupError:
        return None


def _retention_job():
    from apps.core import scheduler

    return scheduler.get_job("governance.retention")


def _field(model, *hints, kind=None):
    for f in model._meta.concrete_fields:
        if any(h in f.name for h in hints) and (kind is None or f.get_internal_type() in kind):
            return f
    return None


def _registered_targets() -> list[str]:
    """Retention targets as built: registered at runtime (apps.governance.retention), not model choices."""
    from apps.governance import retention

    return sorted(getattr(retention, "_targets", {}))


def _make_policies(world, *, dry_run: bool) -> list:
    """One active policy per REGISTERED target with the shortest window (retain_days has a DB minimum of 1)."""
    from apps.governance import retention

    Model = _retention_model()
    targets = _registered_targets()
    if not targets:
        pytest.skip("no retention target registered")
    created = []
    with world.tenant():
        for key in targets:
            action = "delete" if "delete" in retention.get_target(key).actions else retention.get_target(key).actions[0]
            created.append(Model.objects.create(org_id=world.id, name=f"qa {key}", target=key, retain_days=1,
                                                action=action, mode="dry_run" if dry_run else "enforce",
                                                is_active=True, created_by=world.owner.pk))
    return created


def _run_retention(world, *, dry_run: bool) -> None:
    """Run the periodic job body for the org 30 days in the FUTURE, so every seeded row is past the cutoff (the
    strongest case). `run_retention` iterates orgs on the ops connection, which cannot see this test's
    uncommitted rows, so the per-org body runs directly inside the org's tenant context."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.governance import retention

    with world.tenant():
        out = retention.run_for_current_org(now=timezone.now() + timedelta(days=30))
    errors = {k: v["error"] for k, v in out.items() if v.get("error")}
    assert errors == {}, f"retention policies failed: {errors}"
    applied = [v for v in out.values() if v.get("applied")]
    assert bool(applied) is (not dry_run), out


def _seed_activity(world, capture) -> None:
    owner = api_client_for(world.owner)
    upload_and_ingest(owner, world.slug, "sales.csv", SALES.read_bytes(), capture, name="Sales")
    board = owner.post(f"/api/v1/orgs/{world.slug}/dashboards", {"title": "retention probe"}, format="json")
    assert board.status_code in (201, 404), board.content[:200]


needs_retention = pytest.mark.skipif(
    not tenant_route_exists("retention-policies"), reason="retention-policies not routed yet (Backend W4)"
)


@needs_retention
def test_dry_run_changes_nothing(two_orgs, memory_storage, fake_redis, fake_embedder,
                                django_capture_on_commit_callbacks):
    if _retention_model() is None or _retention_job() is None:
        pytest.skip("RetentionPolicy / governance.retention job not built yet")
    a = two_orgs.a
    _seed_activity(a, django_capture_on_commit_callbacks)
    _make_policies(a, dry_run=True)
    tables = _tenant_tables()
    before = _counts(a, tables)
    _run_retention(a, dry_run=True)
    after = _counts(a, tables)
    changed = {t: (before[t], after[t]) for t in tables if before[t] != after[t]}
    policy_tables = {_retention_model()._meta.db_table}
    changed = {t: v for t, v in changed.items() if t not in policy_tables and "audit" not in t}
    assert changed == {}, f"a dry run changed data: {changed}"


@needs_retention
def test_a_retention_run_never_deletes_append_only_rows(two_orgs, memory_storage, fake_redis, fake_embedder,
                                                        django_capture_on_commit_callbacks):
    if _retention_model() is None or _retention_job() is None:
        pytest.skip("RetentionPolicy / governance.retention job not built yet")
    a, b = two_orgs
    _seed_activity(a, django_capture_on_commit_callbacks)
    strict = _app_readable(_strict_tables())
    before_a, before_b = _counts(a, strict), _counts(b, strict)
    assert sum(before_a.values()) > 0, "the seeded activity produced no append-only rows (audit)"
    _make_policies(a, dry_run=False)
    _run_retention(a, dry_run=False)
    after_a, after_b = _counts(a, strict), _counts(b, strict)
    lost = {t: (before_a[t], after_a[t]) for t in strict if after_a[t] < before_a[t]}
    assert lost == {}, f"retention deleted append-only rows: {lost}"
    assert all(after_b[t] >= before_b[t] for t in strict), "retention of org A touched org B"


@needs_retention
def test_retention_api_refuses_append_only_targets(two_orgs):
    a = two_orgs.a
    owner = api_client_for(a.owner)
    for target in ("audit_events", "audit_event", "core_auditevent", "bi_query_audit", "platform_audit_event"):
        resp = owner.post(f"/api/v1/orgs/{a.slug}/retention-policies",
                          {"target": target, "retention_days": 1, "action": "delete"}, format="json")
        assert resp.status_code in (400, 422), f"{target}: {resp.status_code} {resp.content[:200]}"
    viewer = api_client_for(a.viewer)  # governance.retention.edit only
    assert viewer.get(f"/api/v1/orgs/{a.slug}/retention-policies").status_code in (403, 404)
    assert api_client_for(two_orgs.b.owner).get(f"/api/v1/orgs/{a.slug}/retention-policies").status_code == 404


# ------------------------------------------------------------------------------------------------ usage
def _dataset_count(body) -> int | None:
    from testing.ml import pick

    value = pick(body, "datasets.count", "datasets.total", "totals.datasets", "summary.datasets", "datasets",
                 default=None)
    if isinstance(value, int | float):
        return int(value)
    return None


@pytest.mark.skipif(not tenant_route_exists("usage"), reason="/orgs/<org>/usage not routed yet (Backend W4)")
def test_usage_is_tenant_scoped(two_orgs, memory_storage, fake_redis, fake_embedder,
                                django_capture_on_commit_callbacks):
    a, b = two_orgs
    capture = django_capture_on_commit_callbacks
    upload_and_ingest(api_client_for(a.owner), a.slug, "a.csv", b"k,v\nx,1\n", capture, name="A only")
    b_owner = api_client_for(b.owner)
    for i in range(3):
        upload_and_ingest(b_owner, b.slug, f"b{i}.csv", b"k,v\ny,2\n", capture, name=f"B {i}")
    for suffix in ("usage", "ai/usage"):
        if not tenant_route_exists(suffix):
            continue
        url_a = f"/api/v1/orgs/{a.slug}/{suffix}"
        resp = api_client_for(a.owner).get(url_a)
        assert resp.status_code == 200, (suffix, resp.content[:300])
        text = json.dumps(resp.json())
        leaked = sorted(i for i in b.identifiers() if i and i in text)
        assert leaked == [], f"{suffix}: org B identifiers in org A's usage: {leaked}"
        assert api_client_for(b.owner).get(url_a).status_code == 404
        assert api_client_for(two_orgs.outsider).get(url_a).status_code == 404
        assert api_client_for(None).get(url_a).status_code == 401
        if suffix == "usage":
            n = _dataset_count(resp.json())
            if n is not None:
                assert n == 1, f"org A has 1 dataset, usage reports {n} (B has 3)"
            nb = _dataset_count(b_owner.get(f"/api/v1/orgs/{b.slug}/usage").json())
            if nb is not None:
                assert nb == 3
