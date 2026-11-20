"""W4 governance: retention policies (CRUD, dry run first, apply, legal hold, periodic job), the policy-intersection
used by the preview sample, and the retention target registry. Unit tests need no DB; the rest run as nexora_app."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.core.models import AuditEvent, Job, Notification
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, member_of, org_url
from apps.governance import retention, services
from apps.governance.models import RetentionPolicy
from apps.governance.serializers import RetentionPolicySerializer
from nexora.contracts import MaskSpec, QueryPolicy, RowPredicate

DS = uuid4()
ORG = uuid4()


# ================================================================================================ unit (no DB)
def _policy(allowed, masks=None, rows=None, cap=1000, fp="a"):
    return QueryPolicy(org_id=ORG, user_id=uuid4(), dataset_id=DS, tables={"sales": "s.t", "returns": "s.r"},
                       allowed_columns=allowed, column_masks=masks or {}, row_predicates=rows or {}, row_cap=cap,
                       fingerprint=fp)


def test_intersect_policies_is_the_most_restrictive_of_both():
    member = _policy({"sales": frozenset({"region", "amount", "email"}), "returns": frozenset({"id"})},
                     masks={("sales", "email"): MaskSpec("mask_partial", 4)},
                     rows={"sales": RowPredicate((("region", "in", ("North",)),))}, cap=500, fp="m")
    admin = _policy({"sales": frozenset({"region", "email", "margin"})},
                    masks={("sales", "email"): MaskSpec("mask_partial", 2), ("sales", "region"): MaskSpec("mask_hash")},
                    rows={"sales": RowPredicate((("amount", ">", 0), ("region", "in", ("North",))))}, cap=800, fp="x")
    got = services.intersect_policies(member, admin)
    assert got.allowed_columns == {"sales": frozenset({"region", "email"})}  # amount (admin) and returns dropped
    assert got.tables == {"sales": "s.t"}
    assert got.column_masks == {("sales", "email"): MaskSpec("mask_partial", 2), ("sales", "region"): MaskSpec("mask_hash")}
    assert got.row_predicates["sales"].conditions == (("region", "in", ("North",)), ("amount", ">", 0))
    assert got.row_cap == 500 and got.user_id == member.user_id and got.fingerprint not in ("m", "x")


def test_intersect_policies_mask_rank_and_dataset_mismatch():
    a = _policy({"sales": frozenset({"c"})}, masks={("sales", "c"): MaskSpec("mask_partial", 1)})
    b = _policy({"sales": frozenset({"c"})}, masks={("sales", "c"): MaskSpec("mask_null")})
    assert services.intersect_policies(a, b).column_masks[("sales", "c")].kind == "mask_null"
    other = QueryPolicy(org_id=ORG, user_id=None, dataset_id=uuid4(), tables={}, allowed_columns={})
    with pytest.raises(ValueError):
        services.intersect_policies(a, other)


def test_registry_refuses_append_only_targets_and_bad_actions():
    for key in ("audit_events", "access_history", "bi.query_audit", "ai.request_logs"):
        with pytest.raises(ValueError):
            retention.register_target(retention.RetentionTarget(key, "x", "x", ("delete",), lambda c, a: 0,
                                                                lambda c, a: 0))
    with pytest.raises(ValueError):
        retention.register_target(retention.RetentionTarget("unit.t", "x", "x", ("purge",), lambda c, a: 0,
                                                            lambda c, a: 0))


def test_supported_targets_are_registered_by_their_apps():
    keys = {t.key for t in retention.targets()}
    assert {"notifications", "jobs", "bi.conversations"} <= keys
    assert retention.get_target("bi.conversations").actions == ("delete", "archive")
    assert not keys & retention.NEVER_TARGETS


def test_the_periodic_job_is_registered():
    from apps.core.scheduler import get_job

    job = get_job("governance.retention")
    assert job is not None and job.every_seconds == 3600 and job.fn is retention.run_retention


@pytest.mark.parametrize("data, field", [
    ({"name": "x", "target": "audit_events", "retain_days": 30}, "target"),
    ({"name": "x", "target": "nope", "retain_days": 30}, "target"),
    ({"name": "x", "target": "notifications", "retain_days": 0}, "retain_days"),
    ({"name": "x", "target": "notifications", "retain_days": 99999}, "retain_days"),
    ({"name": "x", "target": "notifications", "retain_days": 30, "action": "archive"}, "action"),
])
def test_serializer_validation(data, field):
    ser = RetentionPolicySerializer(data=data)
    assert not ser.is_valid() and field in ser.errors


class _FakePolicy(SimpleNamespace):
    def save(self, update_fields=None):
        self.saved = (getattr(self, "saved", 0) or 0) + 1


@pytest.fixture
def unit_target(monkeypatch):
    seen = {"apply": [], "count": []}
    t = retention.RetentionTarget("unit.target", "Unit", "", ("delete", "archive"),
                                  count=lambda cutoff, action: seen["count"].append((cutoff, action)) or 7,
                                  apply=lambda cutoff, action: seen["apply"].append((cutoff, action)) or 5)
    retention.register_target(t)
    monkeypatch.setattr(retention, "legal_hold_active", lambda: False)
    monkeypatch.setattr(retention.audit, "record", lambda *a, **k: seen.setdefault("audit", []).append((a, k)))
    monkeypatch.setattr(retention.transaction, "atomic", lambda *a, **k: _Null())
    yield seen
    retention.unregister_target("unit.target")


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_apply_requires_a_dry_run_then_runs_with_the_cutoff(unit_target):
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    p = _FakePolicy(target="unit.target", retain_days=30, action="delete", last_dry_run_at=None)
    with pytest.raises(retention.RetentionError) as exc:
        retention.apply(p, now=now)
    assert exc.value.code == "retention_dry_run_required"
    dry = retention.dry_run(p, now=now)
    assert dry["matched"] == 7 and dry["cutoff"] == (now - timedelta(days=30)).isoformat()
    assert p.last_dry_run_at == now and not unit_target["apply"]  # a dry run changes nothing
    result = retention.apply(p, trigger="schedule", now=now)
    assert result["affected"] == 5 and result["trigger"] == "schedule"
    assert unit_target["apply"] == [(now - timedelta(days=30), "delete")]
    (args, kwargs), = unit_target["audit"]
    assert args == ("governance.retention.apply",) and kwargs["actor"] == ("system", None)


def test_legal_hold_blocks_apply_but_not_dry_run(unit_target, monkeypatch):
    monkeypatch.setattr(retention, "legal_hold_active", lambda: True)
    p = _FakePolicy(target="unit.target", retain_days=1, action="archive", last_dry_run_at=None)
    assert retention.dry_run(p)["legal_hold"] is True
    result = retention.apply(p)
    assert result == {**result, "affected": 0, "skipped": "legal_hold"} and not unit_target["apply"]


def test_unsupported_target_and_action():
    p = _FakePolicy(target="gone", retain_days=1, action="delete", last_dry_run_at=timezone.now())
    with pytest.raises(retention.RetentionError):
        retention.dry_run(p)
    with pytest.raises(retention.RetentionError) as exc:
        retention.apply(_FakePolicy(target="notifications", retain_days=1, action="archive",
                                    last_dry_run_at=timezone.now()))
    assert exc.value.code == "retention_action_unsupported"


# ================================================================================================ DB
def rp_url(org, path=""):
    return org_url(org, f"retention-policies{path}")


def _old_notification(user, days_old: int, title="n"):
    n = Notification.objects.create(user_id=user.pk, kind="pipeline.run_failed", title=title)
    Notification.objects.filter(id=n.id).update(created_at=timezone.now() - timedelta(days=days_old))
    return n


@pytest.mark.django_db
def test_retention_crud_permissions_and_dry_run_first(org, owner, owner_client):
    assert owner_client.get(org_url(org, "retention-targets")).json()["items"]
    r = owner_client.post(rp_url(org), {"name": "Notifications 30d", "target": "notifications", "retain_days": 30},
                          format="json")
    assert r.status_code == 201, r.content
    pid = r.json()["id"]
    assert r.json()["mode"] == "dry_run" and r.json()["last_dry_run_at"] is None
    assert owner_client.post(rp_url(org), {"name": "dup", "target": "notifications", "retain_days": 5},
                             format="json").status_code == 409
    assert owner_client.post(rp_url(org), {"name": "x", "target": "jobs", "retain_days": 5, "mode": "enforce"},
                             format="json").json()["code"] == "retention_dry_run_required"
    r = owner_client.patch(rp_url(org, f"/{pid}"), {"mode": "enforce"}, format="json")
    assert r.status_code == 409 and r.json()["code"] == "retention_dry_run_required"
    r = owner_client.post(rp_url(org, f"/{pid}/apply"), {"confirm": True}, format="json")
    assert r.status_code == 409 and r.json()["code"] == "retention_dry_run_required"

    with tenant_context(org.id):
        _old_notification(owner, 40, "old")
        _old_notification(owner, 1, "new")
    r = owner_client.post(rp_url(org, f"/{pid}/dry-run"))
    assert r.status_code == 200 and r.json()["dry_run"]["matched"] == 1
    with tenant_context(org.id):
        assert Notification.objects.count() == 2  # a dry run deletes nothing
    assert owner_client.patch(rp_url(org, f"/{pid}"), {"mode": "enforce"}, format="json").status_code == 200
    # changing the window invalidates the dry run
    r = owner_client.patch(rp_url(org, f"/{pid}"), {"retain_days": 10, "mode": "enforce"}, format="json")
    assert r.status_code == 409
    assert owner_client.post(rp_url(org, f"/{pid}/apply"), {}, format="json").status_code == 400  # confirm needed
    owner_client.post(rp_url(org, f"/{pid}/dry-run"))
    r = owner_client.post(rp_url(org, f"/{pid}/apply"), {"confirm": True}, format="json")
    assert r.status_code == 200 and r.json()["result"]["affected"] == 1
    with tenant_context(org.id):
        assert list(Notification.objects.values_list("title", flat=True)) == ["new"]
        actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"governance.retention.create", "governance.retention.dry_run", "governance.retention.update",
            "governance.retention.apply"} <= actions
    listed = owner_client.get(rp_url(org)).json()
    assert [p["id"] for p in listed["items"]] == [pid] and "next_cursor" in listed
    assert owner_client.delete(rp_url(org, f"/{pid}")).status_code == 204
    assert owner_client.get(rp_url(org, f"/{pid}")).status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["analyst", "viewer", "auditor", "data_engineer"])
def test_retention_needs_governance_retention_edit(org, role):
    client = client_for(member_of(org, role))
    assert client.get(rp_url(org)).status_code == 403
    assert client.post(rp_url(org), {"name": "x", "target": "jobs", "retain_days": 5},
                       format="json").status_code == 403


@pytest.mark.django_db
def test_append_only_targets_are_refused(org, owner_client):
    for target in ("audit_events", "access_history", "bi.query_audit"):
        r = owner_client.post(rp_url(org), {"name": "x", "target": target, "retain_days": 5}, format="json")
        assert r.status_code == 400 and r.json()["errors"][0]["field"] == "target"


@pytest.mark.django_db
def test_jobs_target_never_touches_running_jobs(org, owner_client):
    old = timezone.now() - timedelta(days=100)
    with tenant_context(org.id):
        done = Job.objects.create(type="unit", status="succeeded", finished_at=old)
        Job.objects.create(type="unit", status="running")
        Job.objects.create(type="unit", status="failed", finished_at=timezone.now())
    pid = owner_client.post(rp_url(org), {"name": "jobs", "target": "jobs", "retain_days": 30},
                            format="json").json()["id"]
    assert owner_client.post(rp_url(org, f"/{pid}/dry-run")).json()["dry_run"]["matched"] == 1
    owner_client.post(rp_url(org, f"/{pid}/apply"), {"confirm": True}, format="json")
    with tenant_context(org.id):
        assert not Job.objects.filter(id=done.id).exists() and Job.objects.count() == 2


@pytest.mark.django_db
def test_bi_conversations_archive_keeps_text_and_drops_rows(org, owner, owner_client):
    from apps.bi.models import Conversation, Message

    with tenant_context(org.id):
        conv = Conversation.objects.create(user_id=owner.pk, dataset_id=uuid4(), title="old",
                                           last_message_at=timezone.now() - timedelta(days=200))
        msg = Message.objects.create(conversation=conv, role="assistant", content="Revenue by region",
                                     result={"columns": [{"name": "r"}], "rows": [["North"]], "row_count": 1})
        fresh = Conversation.objects.create(user_id=owner.pk, dataset_id=uuid4(), title="fresh",
                                            last_message_at=timezone.now())
    pid = owner_client.post(rp_url(org), {"name": "bi", "target": "bi.conversations", "retain_days": 90,
                                          "action": "archive"}, format="json").json()["id"]
    assert owner_client.post(rp_url(org, f"/{pid}/dry-run")).json()["dry_run"]["matched"] == 1
    assert owner_client.post(rp_url(org, f"/{pid}/apply"), {"confirm": True},
                             format="json").json()["result"]["affected"] == 1
    with tenant_context(org.id):
        msg.refresh_from_db()
        assert msg.content == "Revenue by region" and msg.result["rows"] == [] and msg.result["archived"] is True
        assert msg.result["row_count"] == 1
    owner_client.patch(rp_url(org, f"/{pid}"), {"action": "delete"}, format="json")
    owner_client.post(rp_url(org, f"/{pid}/dry-run"))
    assert owner_client.post(rp_url(org, f"/{pid}/apply"), {"confirm": True},
                             format="json").json()["result"]["affected"] == 1
    with tenant_context(org.id):
        assert list(Conversation.objects.values_list("id", flat=True)) == [fresh.id]
        assert not Message.objects.filter(id=msg.id).exists()


@pytest.mark.django_db
def test_legal_hold_blocks_deletion(org, owner, owner_client):
    from apps.orgs.models import Organization

    with tenant_context(org.id):
        Organization.objects.filter(id=org.id).update(settings={"legal_hold": True})
        _old_notification(owner, 40)
    pid = owner_client.post(rp_url(org), {"name": "n", "target": "notifications", "retain_days": 30},
                            format="json").json()["id"]
    owner_client.post(rp_url(org, f"/{pid}/dry-run"))
    r = owner_client.post(rp_url(org, f"/{pid}/apply"), {"confirm": True}, format="json")
    assert r.json()["result"]["skipped"] == "legal_hold"
    with tenant_context(org.id):
        assert Notification.objects.count() == 1


@pytest.mark.django_db
def test_periodic_run_dry_runs_all_and_applies_only_enforced(org, owner):
    with tenant_context(org.id):
        _old_notification(owner, 40)
        Job.objects.create(type="unit", status="succeeded", finished_at=timezone.now() - timedelta(days=90))
        enforced = RetentionPolicy.objects.create(name="n", target="notifications", retain_days=30, mode="enforce",
                                                  last_dry_run_at=timezone.now())
        dry = RetentionPolicy.objects.create(name="j", target="jobs", retain_days=30)
        out = retention.run_for_current_org()
        assert out[str(enforced.id)]["applied"]["affected"] == 1
        assert out[str(dry.id)]["applied"] is None and out[str(dry.id)]["dry_run"]["matched"] == 1
        assert Notification.objects.count() == 0 and Job.objects.count() == 1
        ev = AuditEvent.objects.get(action="governance.retention.apply")
        assert ev.actor_type == "system" and ev.metadata["trigger"] == "schedule"


@pytest.mark.django_db
def test_retention_rows_are_tenant_isolated(org, other_org, owner_client):
    with tenant_context(other_org.id):
        foreign = RetentionPolicy.objects.create(name="x", target="jobs", retain_days=5)
    assert owner_client.get(rp_url(org, f"/{foreign.id}")).status_code == 404
    assert owner_client.post(rp_url(org, f"/{foreign.id}/dry-run")).status_code == 404
    assert owner_client.get(rp_url(org)).json()["items"] == []


@pytest.mark.django_db(transaction=True, databases="__all__")
def test_run_retention_finds_orgs_through_the_ops_alias(org, owner):
    with tenant_context(org.id):
        _old_notification(owner, 40)
        RetentionPolicy.objects.create(name="n", target="notifications", retain_days=30, mode="enforce",
                                       last_dry_run_at=timezone.now())
    assert org.id in retention.orgs_with_active_policies()
    summary = retention.run_retention()
    (entry,) = summary[str(org.id)].values()
    assert entry["applied"]["affected"] == 1
