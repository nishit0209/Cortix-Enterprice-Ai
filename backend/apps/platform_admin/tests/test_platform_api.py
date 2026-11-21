"""W4 platform super-admin (API_SPECIFICATION Phase 11): /api/v1/platform/... for `User.is_platform_admin` only.

Unit tests need no DB. The API tests are TRANSACTIONAL (`databases="__all__"`): the platform services read and write
on the `ops` alias, a separate connection (nexora_ops, BYPASSRLS) that cannot see a test transaction's rows.
They prove: everyone but a platform admin gets 404; payloads carry counts/status only (never tenant row values);
suspend -> members get 403 `org_suspended`, API keys 401, reactivate restores access; every action lands in the
append-only PlatformAuditEvent (which the app role cannot even read); model quotas; the log ring; the command.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.core import jobs, runner
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, make_org, make_user, member_of, org_url
from apps.platform_admin import logs as logmod
from apps.platform_admin import services
from apps.platform_admin.models import AppLogRecord, PlatformAuditEvent
from apps.platform_admin.views import IsPlatformAdmin, LogsQuery

SECRET = "SYNTHETIC-TENANT-SECRET-7731"
P = "/api/v1/platform/"


# ================================================================================================ unit (no DB)
def test_reachability_from_the_latest_real_requests():
    now = timezone.now()
    assert services.reachability(False, now, None) == "not_configured"
    assert services.reachability(True, None, None) == "unknown"
    assert services.reachability(True, now, now - timedelta(minutes=1)) == "yes"
    assert services.reachability(True, now - timedelta(minutes=1), now) == "failing"
    assert services.reachability(True, None, now) == "failing"


def test_is_platform_admin_answers_404_for_everyone_else():
    perm = IsPlatformAdmin()
    for user in (None, SimpleNamespace(is_authenticated=False, is_platform_admin=True),
                 SimpleNamespace(is_authenticated=True, is_platform_admin=False, is_active=True),
                 SimpleNamespace(is_authenticated=True, is_platform_admin=True, is_active=False)):
        with pytest.raises(NotFound):
            perm.has_permission(SimpleNamespace(user=user), None)
    admin = SimpleNamespace(is_authenticated=True, is_platform_admin=True, is_active=True)
    assert perm.has_permission(SimpleNamespace(user=admin), None) is True


def test_logs_query_normalises_and_validates_the_level():
    ok = LogsQuery(data={"level": "error", "component": "apps.bi"})
    assert ok.is_valid(), ok.errors
    assert ok.validated_data["level"] == "ERROR" and ok.validated_data["limit"] == 100
    assert not LogsQuery(data={"level": "loud"}).is_valid()
    assert not LogsQuery(data={"component": "a b;drop"}).is_valid()


def _record(name="apps.bi.services", level=logging.WARNING, msg="disk %s low", args=("C:",), exc=None):
    return logging.LogRecord(name, level, "/x/services.py", 42, msg, args, exc)


def test_log_handler_rows_skip_list_and_bounded_queue(monkeypatch):
    handler = logmod.DatabaseLogHandler(start_thread=False)
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        handler.emit(_record(exc=sys.exc_info()))
    row = handler.drain()[0]
    assert row["message"] == "disk C: low" and row["level_name"] == "WARNING" and row["exc_type"] == "ValueError"
    assert row["logger"] == "apps.bi.services" and row["where"].endswith(":42")
    handler.emit(_record(name="django.db.backends"))  # never captured (a failing write would log again)
    assert handler.drain() == []
    monkeypatch.setattr(logmod, "QUEUE_SIZE", 2)
    small = logmod.DatabaseLogHandler(start_thread=False)
    small.queue.maxsize = 2
    for _ in range(5):
        small.emit(_record())
    assert small.queue.qsize() == 2 and small.dropped == 3


def test_log_capture_is_off_under_test_settings_and_on_by_flag(settings):
    assert logmod.enabled() is False
    settings.PLATFORM_LOG_TO_DB = True
    assert logmod.enabled() is True
    settings.PLATFORM_LOG_MAX_ROWS = 10
    assert logmod.max_rows() == 100  # floor


def test_pool_status_reports_the_runner(settings):
    settings.TASK_RUNNER = "eager"
    assert runner.pool_status() == {"runner": "eager"}
    settings.TASK_RUNNER = "thread"
    st = runner.pool_status()
    assert st["runner"] == "thread" and st["threads"] >= 1 and "pending" in st


# ================================================================================================ DB (transactional)
db_all = pytest.mark.django_db(transaction=True, databases="__all__")


def _admin():
    return make_user(full_name="Platform Admin", is_platform_admin=True)


def _world():
    owner = make_user(full_name="Tenant Owner")
    org = make_org(owner, name="Platform Synthetic Org")
    analyst = member_of(org, "analyst", full_name="Tenant Analyst")
    with tenant_context(org.id):
        job = jobs.create_job("dataset.ingest", user_id=owner.pk)
        jobs.fail(job.id, code="ingest_failed", message=f"row value {SECRET}")
        from apps.core import audit

        audit.record("dataset.created", target_type="datasets.dataset", target_id=str(uuid4()),
                     metadata={"name": SECRET})
    return org, owner, analyst


ROUTES = [("get", "orgs"), ("get", "orgs/{id}"), ("post", "orgs/{id}/suspend"), ("post", "orgs/{id}/reactivate"),
          ("get", "health"), ("get", "jobs"), ("get", "ai/usage"), ("put", "ai/model-quotas/gemini-2.5-flash"),
          ("get", "logs")]


@db_all
def test_every_platform_route_is_404_for_non_admins_and_anonymous():
    org, owner, analyst = _world()
    for user in (None, owner, analyst):
        client = client_for(user)
        for method, path in ROUTES:
            resp = getattr(client, method)(P + path.format(id=org.id), {"reason": "x", "rpm": 1, "tpm": 1, "rpd": 1},
                                           format="json")
            assert resp.status_code == 404, (user, method, path, resp.status_code)
    with transaction.atomic(using="ops"):
        assert not PlatformAuditEvent.objects.using("ops").exists()
    from apps.orgs.models import Organization

    assert Organization.objects.using("ops").get(id=org.id).status == "active"  # nothing happened


@db_all
def test_orgs_list_and_detail_are_counts_and_status_only():
    org, owner, analyst = _world()
    admin = client_for(_admin())
    resp = admin.get(P + "orgs", {"search": "Platform Synthetic"})
    assert resp.status_code == 200, resp.content
    (item,) = [i for i in resp.json()["items"] if i["id"] == str(org.id)]
    assert item["members_count"] == 2 and item["datasets_count"] == 0 and item["status"] == "active"
    assert set(item) == {"id", "name", "slug", "status", "plan", "region", "created_at", "members_count",
                         "datasets_count", "storage_bytes", "ai_usage"}
    detail = admin.get(P + f"orgs/{org.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["members_by_role"] == {"analyst": 1, "owner": 1}
    assert body["jobs_by_status"] == {"failed": 1} and body["last_activity_at"]
    for payload in (resp.content, detail.content):  # never tenant row values, member emails or names
        text = payload.decode()
        assert SECRET not in text and owner.email not in text and analyst.email not in text
    assert admin.get(P + f"orgs/{uuid4()}").status_code == 404
    assert admin.get(P + "orgs", {"status": "bogus"}).status_code == 400


@db_all
def test_suspend_blocks_members_and_api_keys_then_reactivate_restores_access():
    from apps.accounts.services import create_api_key

    org, owner, analyst = _world()
    admin_user = _admin()
    admin = client_for(admin_user)
    with tenant_context(org.id):
        _key, raw = create_api_key(name="ci", scopes=["datasets.view"], created_by=owner)
    member = client_for(analyst)
    assert member.get(org_url(org, "jobs")).status_code == 200

    assert admin.post(P + f"orgs/{org.id}/suspend", {}, format="json").status_code == 400  # reason required
    resp = admin.post(P + f"orgs/{org.id}/suspend", {"reason": "Synthetic abuse report"}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "suspended" and resp.json()["suspended_reason"] == "Synthetic abuse report"
    denied = member.get(org_url(org, "jobs"))
    assert denied.status_code == 403 and denied.json()["code"] == "org_suspended"
    assert client_for(make_user()).get(org_url(org, "jobs")).status_code == 404  # non-members learn nothing
    keyed = client_for(None)
    keyed.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert keyed.get(org_url(org, "datasets")).status_code == 401
    assert admin.post(P + f"orgs/{org.id}/suspend", {"reason": "again"}, format="json").status_code == 409

    events = list(PlatformAuditEvent.objects.using("ops").filter(target_id=str(org.id)).order_by("occurred_at"))
    assert [e.action for e in events] == ["org.suspend"]
    assert events[0].actor_id == admin_user.pk and events[0].metadata["reason"] == "Synthetic abuse report"
    with tenant_context(org.id):
        from apps.core.models import AuditEvent

        assert AuditEvent.objects.filter(action="org.suspended").count() == 1  # the org's own trail too

    resp = admin.post(P + f"orgs/{org.id}/reactivate", {"reason": "resolved"}, format="json")
    assert resp.status_code == 200 and resp.json()["status"] == "active"
    assert member.get(org_url(org, "jobs")).status_code == 200
    assert [a["action"] for a in admin.get(P + f"orgs/{org.id}").json()["platform_actions"]] == [
        "org.reactivate", "org.suspend"]


@db_all
def test_platform_audit_is_append_only_and_hidden_from_the_app_role():
    services.platform_audit("unit.probe", target_type="t", target_id="1")
    event = PlatformAuditEvent.objects.using("ops").get(action="unit.probe")
    with pytest.raises(DatabaseError), transaction.atomic(using="ops"):
        PlatformAuditEvent.objects.using("ops").filter(id=event.id).delete()
    for table in ("platform_admin_platformauditevent", "platform_admin_applogrecord"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:  # nexora_app
            cur.execute(f"SELECT count(*) FROM {table}")


@db_all
def test_jobs_overview_reports_counts_and_error_codes_only():
    org, owner, _ = _world()
    with tenant_context(org.id):
        jobs.create_job("ml.train", user_id=owner.pk)
    admin = client_for(_admin())
    body = admin.get(P + "jobs").json()
    assert body["by_status"]["failed"] >= 1 and body["queue"]["by_type"].get("ml.train") == 1
    assert body["failures_24h"] >= 1 and body["runner"]["runner"] == "eager"
    (failed,) = [i for i in body["items"] if i["org_id"] == str(org.id)]
    assert failed["error_code"] == "ingest_failed" and failed["org_slug"] == org.slug
    assert SECRET not in json.dumps(body)
    ml = admin.get(P + "jobs", {"queue": "ml", "status": "queued"}).json()
    assert [i["type"] for i in ml["items"]] == ["ml.train"]
    assert admin.get(P + "jobs", {"status": "nope"}).status_code == 400


@db_all
def test_ai_usage_and_model_quota_put():
    from apps.ai.models import AIModelQuota, AIUsageDaily

    org, *_ = _world()
    with tenant_context(org.id):
        AIUsageDaily.objects.create(day=timezone.now().date(), model="gemini-2.5-flash-lite", requests=7,
                                    input_tokens=100, output_tokens=50)
    admin_user = _admin()
    admin = client_for(admin_user)
    body = admin.get(P + "ai/usage", {"days": 3}).json()
    assert body["totals"]["requests"] == 7 and body["totals"]["input_tokens"] == 100
    assert body["top_orgs"] == [{"org_id": str(org.id), "org_slug": org.slug, "requests": 7}]
    assert body["configured"] is False  # tests never configure Gemini

    resp = admin.put(P + "ai/model-quotas/gemini-2.5-flash", {"rpm": 10, "tpm": 250000, "rpd": 250,
                                                              "notes": "AI Studio 2026-11"}, format="json")
    assert resp.status_code == 200, resp.content
    got = resp.json()
    assert got["configured"] and got["rpm"] == 10 and got["effective"] == {"rpm": 9, "tpm": 225000, "rpd": 225}
    assert AIModelQuota.objects.get(model="gemini-2.5-flash").rpd == 250
    event = PlatformAuditEvent.objects.using("ops").get(action="ai.model_quota.set")
    assert event.actor_id == admin_user.pk and event.metadata["after"]["rpm"] == 10
    assert admin.put(P + "ai/model-quotas/Bad Model!", {"rpm": 1, "tpm": 1, "rpd": 1},
                     format="json").status_code == 400
    assert admin.put(P + "ai/model-quotas/gemini-x", {"rpm": 0, "tpm": 1, "rpd": 1}, format="json").status_code == 400


@db_all
def test_health_reports_every_component():
    body = client_for(_admin()).get(P + "health").json()
    comps = body["components"]
    assert comps["database"]["status"] == "ok" and comps["database_ops"]["status"] == "ok"
    assert {"cache", "storage", "task_runner", "scheduler", "gemini", "embeddings"} <= set(comps)
    assert comps["gemini"]["configured"] is False and comps["gemini"]["reachable"] == "not_configured"
    assert comps["scheduler"]["mode"] == "disabled" and comps["scheduler"]["status"] == "ok"
    assert any(j["name"] == "governance.retention" for j in comps["scheduler"]["jobs"])


@db_all
def test_log_ring_capture_filter_page_and_trim():
    handler = logmod.DatabaseLogHandler(start_thread=False)
    for i in range(6):
        handler.emit(_record(name="apps.bi.services", msg="bi warning %s", args=(i,)))
    handler.emit(_record(name="apps.documents.tasks", level=logging.ERROR, msg="parse failed", args=()))
    assert handler.flush_now() == 7
    admin = client_for(_admin())
    body = admin.get(P + "logs", {"limit": 4}).json()
    assert [i["message"] for i in body["items"]] == ["parse failed", "bi warning 5", "bi warning 4", "bi warning 3"]
    more = admin.get(P + "logs", {"limit": 4, "before_id": body["next_before_id"]}).json()
    assert [i["message"] for i in more["items"]] == ["bi warning 2", "bi warning 1", "bi warning 0"]
    assert more["next_before_id"] is None
    errors = admin.get(P + "logs", {"level": "error"}).json()["items"]
    assert [(i["level"], i["component"]) for i in errors] == [("ERROR", "apps.documents.tasks")]
    assert len(admin.get(P + "logs", {"component": "apps.bi"}).json()["items"]) == 6
    assert admin.get(P + "logs", {"component": "apps.b"}).json()["items"] == []  # prefix match on dotted names only
    assert logmod.trim(keep=2) == 5
    assert AppLogRecord.objects.using("ops").count() == 2


@db_all
def test_create_platform_admin_command():
    user = make_user(full_name="Future Admin")
    call_command("create_platform_admin", "--email", user.email.upper())
    user.refresh_from_db()
    assert user.is_platform_admin
    call_command("create_platform_admin", "--email", user.email)  # idempotent: no second audit row
    assert PlatformAuditEvent.objects.using("ops").filter(action="platform_admin.grant").count() == 1
    assert client_for(user).get(P + "health").status_code == 200
    call_command("create_platform_admin", "--email", user.email, "--revoke")
    user.refresh_from_db()
    assert not user.is_platform_admin
    assert PlatformAuditEvent.objects.using("ops").filter(action="platform_admin.revoke").count() == 1
    with pytest.raises(CommandError):
        call_command("create_platform_admin", "--email", "nobody@example.test")
    assert "ops" in connections.databases
