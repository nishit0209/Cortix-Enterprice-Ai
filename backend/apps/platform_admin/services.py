"""Public service API of apps.platform_admin (Phase 11 platform super-admin). Other apps import only from here.

Everything here runs on the `ops` DB alias (nexora_ops, BYPASSRLS) and returns COUNTS, SIZES and STATUS only:
never tenant data rows, never row values, never names of datasets/documents/columns, never error messages that a
tenant's data could have produced. Cross-tenant reads use the `.unscoped` managers, which are allowed only in
platform modules (scripts/check_conventions.py).

    org_summaries(orgs) / org_detail(org)        counts per org
    suspend_org / reactivate_org                 Organization.status + PlatformAuditEvent (+ the org's own audit)
    health()                                     component status
    jobs_overview(queue=, status=)               job counts / recent failures (codes only)
    ai_usage(days) / set_model_quota(...)        project-level Gemini usage and the AIModelQuota rows
    recent_logs(level=, component=, ...)         the bounded AppLogRecord ring
    grant_platform_admin(email, revoke=False)    `manage.py create_platform_admin`
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta
from uuid import UUID

from django.apps import apps as django_apps
from django.db import connections, transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from apps.core.exceptions import Conflict, NexoraError

from .models import AppLogRecord, PlatformAuditEvent

log = logging.getLogger(__name__)

OPS = "ops"
AI_WINDOW_DAYS = 30
FAILURE_WINDOW_H = 24
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
REACHED_STATUSES = ("ok", "rate_limited", "quota_exhausted", "invalid_output")


class OrgNotFound(NexoraError):
    status_code = 404
    default_code = "not_found"
    default_detail = "Not found."


def _model(label: str):
    try:
        return django_apps.get_model(label)
    except LookupError:
        return None


def _unscoped(label: str):
    model = _model(label)
    return None if model is None else model.unscoped.using(OPS)


def _by_org(qs, org_ids: list[UUID], **aggregates) -> dict[UUID, dict]:
    if qs is None or not org_ids:
        return {}
    return {row.pop("org_id"): row for row in qs.filter(org_id__in=org_ids).values("org_id").annotate(**aggregates)
            .order_by()}


# ------------------------------------------------------------------------------------------------ audit
def platform_audit(action: str, *, actor=None, target_type: str = "", target_id: str = "",
                   metadata: dict | None = None, request=None) -> PlatformAuditEvent:
    from apps.core.middleware import request_id_var

    ip = (request.META.get("REMOTE_ADDR") or None) if request is not None else None
    return PlatformAuditEvent.objects.using(OPS).create(
        actor_id=getattr(actor, "pk", None), actor_email=getattr(actor, "email", "") or "", action=action,
        target_type=target_type, target_id=str(target_id or ""), metadata=dict(metadata or {}), ip=ip,
        request_id=request_id_var.get() or "",
    )


# ------------------------------------------------------------------------------------------------ orgs
def orgs_queryset(*, status: str | None = None, search: str | None = None):
    from apps.orgs.models import Organization

    qs = Organization.objects.using(OPS).all()
    if status:
        qs = qs.filter(status=status)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(slug__icontains=search))
    return qs


def get_org(org_id):
    from apps.orgs.models import Organization

    try:
        return Organization.objects.using(OPS).get(id=org_id)
    except (Organization.DoesNotExist, ValueError, TypeError) as exc:
        raise OrgNotFound() from exc


def storage_by_org(ids: list[UUID]) -> dict[UUID, int]:
    """Upload bytes (ready) + warehouse version bytes (live datasets) + document bytes, per org."""
    total: dict[UUID, int] = defaultdict(int)
    uploads = _unscoped("datasets.Upload")
    if uploads is not None:
        for org_id, row in _by_org(uploads.filter(status="ready"), ids, b=Sum("size_bytes")).items():
            total[org_id] += int(row["b"] or 0)
    versions = _unscoped("datasets.DatasetVersion")
    if versions is not None:
        live = versions.filter(status__in=("ready", "loading")).exclude(dataset__status="deleted")
        for org_id, row in _by_org(live, ids, b=Sum("byte_size")).items():
            total[org_id] += int(row["b"] or 0)
    documents = _unscoped("documents.Document")
    if documents is not None:
        for org_id, row in _by_org(documents, ids, b=Sum("size_bytes")).items():
            total[org_id] += int(row["b"] or 0)
    return dict(total)


def org_summaries(orgs: Iterable) -> list[dict]:
    """Counts for a page of orgs (a handful of grouped queries, not one per org)."""
    orgs = list(orgs)
    ids = [o.id for o in orgs]
    since = timezone.now() - timedelta(days=AI_WINDOW_DAYS)
    memberships = _unscoped("orgs.Membership")
    members = _by_org(memberships.filter(status="active") if memberships is not None else None, ids, n=Count("id"))
    datasets_qs = _unscoped("datasets.Dataset")
    datasets = _by_org(datasets_qs.exclude(status="deleted") if datasets_qs is not None else None, ids, n=Count("id"))
    storage = storage_by_org(ids)
    ai_qs = _unscoped("ai.AIUsageDaily")
    ai = _by_org(ai_qs.filter(day__gte=since.date()) if ai_qs is not None else None, ids, req=Sum("requests"),
                 inp=Sum("input_tokens"), out=Sum("output_tokens"))
    out = []
    for o in orgs:
        a = ai.get(o.id, {})
        out.append({
            "id": str(o.id), "name": o.name, "slug": o.slug, "status": o.status, "plan": o.plan, "region": o.region,
            "created_at": o.created_at.isoformat(),
            "members_count": int(members.get(o.id, {}).get("n", 0)),
            "datasets_count": int(datasets.get(o.id, {}).get("n", 0)),
            "storage_bytes": int(storage.get(o.id, 0)),
            "ai_usage": {"window_days": AI_WINDOW_DAYS, "requests": int(a.get("req") or 0),
                         "tokens": int((a.get("inp") or 0) + (a.get("out") or 0))},
        })
    return out


def _count_by(label: str, org_id, field: str, **filters) -> dict[str, int]:
    qs = _unscoped(label)
    if qs is None:
        return {}
    return {str(r[field]): int(r["n"]) for r in qs.filter(org_id=org_id, **filters).values(field)
            .annotate(n=Count("id")).order_by(field)}


def org_detail(org) -> dict:
    """One org: the summary plus breakdowns (members per role, datasets/documents/pipelines/jobs by status, AI usage
    per day, last activity, the platform actions on it). Counts and status only."""
    (summary,) = org_summaries([org])
    roles: dict[str, int] = {}
    memberships = _unscoped("orgs.Membership")
    if memberships is not None:
        roles = {r["role__key"]: int(r["n"]) for r in memberships.filter(org_id=org.id, status="active")
                 .values("role__key").annotate(n=Count("id")).order_by("role__key")}
    since = timezone.now() - timedelta(days=AI_WINDOW_DAYS)
    ai_daily = []
    ai_qs = _unscoped("ai.AIUsageDaily")
    if ai_qs is not None:
        ai_daily = [{"day": r["day"].isoformat(), "requests": int(r["req"] or 0),
                     "tokens": int((r["inp"] or 0) + (r["out"] or 0))}
                    for r in ai_qs.filter(org_id=org.id, day__gte=since.date()).values("day")
                    .annotate(req=Sum("requests"), inp=Sum("input_tokens"), out=Sum("output_tokens")).order_by("day")]
    events = _unscoped("core.AuditEvent")
    last_activity = events.filter(org_id=org.id).aggregate(t=Max("occurred_at"))["t"] if events is not None else None
    recent = PlatformAuditEvent.objects.using(OPS).filter(target_type="orgs.organization", target_id=str(org.id))
    suspension = (org.settings or {}).get("suspension") or {}
    return {
        **summary,
        "suspended_reason": suspension.get("reason") if org.status == "suspended" else None,
        "members_by_role": roles,
        "datasets_by_status": _count_by("datasets.Dataset", org.id, "status"),
        "documents_by_status": _count_by("documents.Document", org.id, "status"),
        "pipelines_by_status": _count_by("pipelines.Pipeline", org.id, "status"),
        "jobs_by_status": _count_by("core.Job", org.id, "status"),
        "ai_daily": ai_daily,
        "last_activity_at": last_activity.isoformat() if last_activity else None,
        "platform_actions": [
            {"occurred_at": e.occurred_at.isoformat(), "action": e.action, "actor_email": e.actor_email,
             "reason": (e.metadata or {}).get("reason")}
            for e in recent.order_by("-occurred_at")[:20]
        ],
    }


def _set_status(org, new_status: str, *, actor, reason: str, request, action: str):
    from apps.core import audit
    from apps.core.tenancy import tenant_context
    from apps.orgs.models import Organization

    with transaction.atomic(using=OPS):
        locked = Organization.objects.using(OPS).select_for_update().get(id=org.id)
        if locked.status == "deleted":
            raise Conflict("A deleted organization cannot change status.", code="org_deleted")
        if locked.status == new_status:
            raise Conflict(f"The organization is already {new_status}.", code=f"org_already_{new_status}")
        previous = locked.status
        org_settings = dict(locked.settings or {})
        if new_status == "suspended":
            org_settings["suspension"] = {"reason": reason, "at": timezone.now().isoformat(),
                                          "by": getattr(actor, "email", "")}
        else:
            org_settings.pop("suspension", None)
        Organization.objects.using(OPS).filter(id=org.id).update(status=new_status, settings=org_settings,
                                                               updated_at=timezone.now())
        platform_audit(action, actor=actor, target_type="orgs.organization", target_id=str(org.id),
                       metadata={"reason": reason, "from": previous, "to": new_status, "slug": locked.slug},
                       request=request)
    try:  # the org's own audit trail shows it too (best effort: the platform audit row is the record)
        with tenant_context(org.id):
            audit.record("org.suspended" if new_status == "suspended" else "org.reactivated",
                         target_type="orgs.organization", target_id=str(org.id),
                         actor=("user", getattr(actor, "pk", None)),
                         metadata={"by": "platform_admin", "reason": reason})
    except Exception:  # noqa: BLE001
        log.warning("platform: could not write the org audit event for %s", org.id, exc_info=True)
    return get_org(org.id)


def suspend_org(org, *, actor, reason: str, request=None):
    """Members then get 403 `org_suspended` on every tenant route; the org's API keys stop working (401)."""
    return _set_status(org, "suspended", actor=actor, reason=reason, request=request, action="org.suspend")


def reactivate_org(org, *, actor, reason: str = "", request=None):
    return _set_status(org, "active", actor=actor, reason=reason, request=request, action="org.reactivate")


# ------------------------------------------------------------------------------------------------ health
def _timed(fn) -> dict:
    started = time.monotonic()
    try:
        value = fn()
        return {"status": "ok", **(value or {}), "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"status": "error", "error": type(exc).__name__}


def _db_ping(alias: str):
    def ping():
        with connections[alias].cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {}

    return ping


def reachability(configured: bool, last_ok, last_err) -> str:
    if not configured:
        return "not_configured"
    if last_ok is None and last_err is None:
        return "unknown"
    if last_err is not None and (last_ok is None or last_err > last_ok):
        return "failing"
    return "yes"


def _ai_status() -> dict:
    """Gemini + embeddings through apps.ai services. Reachability is derived from the latest REAL requests (the
    platform never sends a probe: that would spend quota); the breakers come from the shared limiter."""
    from apps.ai import services as ai_services

    configured = bool(ai_services.cfgmod.is_configured())
    out: dict = {"configured": configured, "tier": ai_services.cfgmod.gemini_tier()}
    logs = _unscoped("ai.AIRequestLog")
    last_ok = last_err = None
    if logs is not None:
        since = timezone.now() - timedelta(hours=FAILURE_WINDOW_H)
        by_status = {r["status"]: int(r["n"]) for r in logs.filter(created_at__gte=since).values("status")
                     .annotate(n=Count("id")).order_by()}
        last_ok = logs.filter(status__in=REACHED_STATUSES).aggregate(t=Max("created_at"))["t"]
        last_err = logs.filter(status="error").aggregate(t=Max("created_at"))["t"]
        out.update(requests_24h=sum(by_status.values()), by_status_24h=by_status,
                   last_success_at=last_ok.isoformat() if last_ok else None,
                   last_error_at=last_err.isoformat() if last_err else None)
    out["reachable"] = reachability(configured, last_ok, last_err)
    try:
        engine = ai_services.gemini()
        breakers = {}
        for tier in ("fast", "reasoning"):
            model = ai_services.cfgmod.model_for_tier(tier)
            br = engine.limiter.breaker(model)
            breakers[model] = {"state": br.state, "resets_at": br.resets_at.isoformat() if br.resets_at else None}
        out["breakers"] = breakers
    except Exception as exc:  # noqa: BLE001 - the limiter store may be unavailable
        out["breakers"] = {"error": type(exc).__name__}
    out["embeddings"] = ai_services.embedder_status_safe()
    return out


def _check_status(value: str) -> dict:
    return {"status": "error" if str(value).startswith("error") else "ok", "detail": value}


def health() -> dict:
    from apps.core import runner, scheduler
    from apps.core.views import collect_checks, runtime_info

    checks = collect_checks()
    components: dict[str, dict] = {
        "database": _timed(_db_ping("default")),
        "database_ops": _timed(_db_ping(OPS)),
        "cache": _check_status(checks["cache"]),
        "redis": _check_status(checks["redis"]),
        "storage": _check_status(checks["storage"]),
        "task_runner": {"status": "ok", **runner.pool_status()},
    }
    sch = scheduler.status()
    fresh = sch["seconds_since_tick"] is not None and sch["seconds_since_tick"] < 4 * scheduler.TICK_S + 60
    sch["status"] = "ok" if sch["mode"] == "disabled" or fresh else "stale"
    components["scheduler"] = sch
    try:
        ai = _ai_status()
        embeddings = ai.pop("embeddings")
        components["gemini"] = {"status": "degraded" if ai.get("reachable") == "failing" else "ok", **ai}
        components["embeddings"] = {"status": "ok" if embeddings.get("available") else "unavailable", **embeddings}
    except Exception as exc:  # noqa: BLE001
        components["gemini"] = {"status": "error", "error": type(exc).__name__}
    overall = "ok" if all(c.get("status") in ("ok", "unavailable") for c in components.values()) else "degraded"
    return {"status": overall, "components": components, "runtime": runtime_info(),
            "checked_at": timezone.now().isoformat()}


# ------------------------------------------------------------------------------------------------ jobs
def jobs_overview(*, queue: str | None = None, status: str | None = None, limit: int = 50) -> dict:
    """Job counts by status, the queue (queued + running per type) and recent failures with error CODES only.
    `queue` filters by job-type prefix (e.g. `ml`, `dataset`); `status` lists jobs in that status instead of the
    last 24 h of failures."""
    from apps.core import runner
    from apps.orgs.models import Organization

    jobs = _unscoped("core.Job")
    qs = jobs.filter(type__startswith=queue) if queue else jobs
    since = timezone.now() - timedelta(hours=FAILURE_WINDOW_H)
    by_status = {r["status"]: int(r["n"]) for r in qs.values("status").annotate(n=Count("id")).order_by()}
    queued = {r["type"]: int(r["n"]) for r in qs.filter(status__in=("queued", "running")).values("type")
              .annotate(n=Count("id")).order_by("type")}
    oldest = qs.filter(status="queued").order_by("created_at").values_list("created_at", flat=True).first()
    failures = qs.filter(status="failed", finished_at__gte=since)
    listing = qs.filter(status=status) if status else failures
    rows = list(listing.order_by("-created_at").values("id", "org_id", "type", "status", "created_at", "finished_at",
                                                       "error")[: max(1, min(int(limit), 200))])
    slugs = dict(Organization.objects.using(OPS).filter(id__in={r["org_id"] for r in rows}).values_list("id", "slug"))
    items = [{
        "id": str(r["id"]), "org_id": str(r["org_id"]), "org_slug": slugs.get(r["org_id"]), "type": r["type"],
        "status": r["status"], "created_at": r["created_at"].isoformat(),
        "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
        "error_code": r["error"].get("code") if isinstance(r["error"], dict) else None,
    } for r in rows]
    return {
        "runner": runner.pool_status(),
        "by_status": by_status,
        "queue": {"by_type": queued, "depth": sum(queued.values()),
                  "oldest_queued_at": oldest.isoformat() if oldest else None},
        "failures_24h": failures.count(),
        "items": items,
    }


# ------------------------------------------------------------------------------------------------ AI
def ai_usage(days: int = 7) -> dict:
    """Project-level Gemini usage (quotas are per Google Cloud project): per day/model across all orgs, the top orgs
    by requests, and the AIModelQuota rows with the limiter's effective limits."""
    from apps.ai import services as ai_services
    from apps.orgs.models import Organization

    days = max(1, min(int(days), 90))
    since = timezone.now().date() - timedelta(days=days - 1)
    usage = _unscoped("ai.AIUsageDaily").filter(day__gte=since)
    daily = [{"day": r["day"].isoformat(), "model": r["model"], "requests": int(r["req"] or 0),
              "input_tokens": int(r["inp"] or 0), "output_tokens": int(r["out"] or 0),
              "blocked": int(r["blk"] or 0), "errors": int(r["err"] or 0)}
             for r in usage.values("day", "model").annotate(req=Sum("requests"), inp=Sum("input_tokens"),
                                                            out=Sum("output_tokens"), blk=Sum("blocked"),
                                                            err=Sum("errors")).order_by("day", "model")]
    top = list(usage.values("org_id").annotate(req=Sum("requests")).order_by("-req")[:10])
    slugs = dict(Organization.objects.using(OPS).filter(id__in=[t["org_id"] for t in top]).values_list("id", "slug"))
    return {
        "range": {"from": since.isoformat(), "days": days},
        "totals": {k: sum(d[k] for d in daily)
                   for k in ("requests", "input_tokens", "output_tokens", "blocked", "errors")},
        "daily": daily,
        "top_orgs": [{"org_id": str(t["org_id"]), "org_slug": slugs.get(t["org_id"]), "requests": int(t["req"] or 0)}
                     for t in top],
        "models": model_quotas(),
        "configured": bool(ai_services.cfgmod.is_configured()),
        "tier": ai_services.cfgmod.gemini_tier(),
    }


def _effective_limits(model: str) -> dict | None:
    from apps.ai import services as ai_services

    try:
        limits = ai_services.cfgmod.load_model_limits(model)
        eff = limits.effective() if hasattr(limits, "effective") else limits
        return {"rpm": eff.rpm, "tpm": eff.tpm, "rpd": eff.rpd}
    except Exception:  # noqa: BLE001
        return None


def model_quotas() -> list[dict]:
    from apps.ai import services as ai_services

    Quota = _model("ai.AIModelQuota")
    rows = {q.model: q for q in Quota.objects.using(OPS).all()} if Quota is not None else {}
    names = sorted(set(rows) | {ai_services.cfgmod.model_for_tier(t) for t in ("fast", "reasoning")})
    out = []
    for name in names:
        q = rows.get(name)
        out.append({
            "model": name, "configured": q is not None,
            "rpm": q.rpm if q else None, "tpm": q.tpm if q else None, "rpd": q.rpd if q else None,
            "verified_at": q.verified_at.isoformat() if q and q.verified_at else None,
            "notes": q.notes if q else "", "updated_at": q.updated_at.isoformat() if q else None,
            "effective": _effective_limits(name),
        })
    return out


def set_model_quota(model: str, *, rpm: int, tpm: int, rpd: int, notes: str = "", verified: bool = True,
                    actor=None, request=None) -> dict:
    """Create/update the AIModelQuota row of `model` (values read from AI Studio). Audited."""
    if not MODEL_RE.match(model or ""):
        raise NexoraError("Invalid model name.", code="invalid_model")  # 400
    Quota = _model("ai.AIModelQuota")
    with transaction.atomic(using=OPS):
        before = Quota.objects.using(OPS).filter(model=model).values("rpm", "tpm", "rpd").first()
        _row, created = Quota.objects.using(OPS).update_or_create(
            model=model, defaults={"rpm": rpm, "tpm": tpm, "rpd": rpd, "notes": (notes or "")[:200],
                                   "verified_at": timezone.now() if verified else None})
        platform_audit("ai.model_quota.set", actor=actor, target_type="ai.aimodelquota", target_id=model,
                       metadata={"before": before, "after": {"rpm": rpm, "tpm": tpm, "rpd": rpd},
                                 "created": created}, request=request)
    return next(q for q in model_quotas() if q["model"] == model)


# ------------------------------------------------------------------------------------------------ logs
def recent_logs(*, level: str | None = None, component: str | None = None, before_id: int | None = None,
                limit: int = 100) -> dict:
    from . import logs as logmod

    qs = AppLogRecord.objects.using(OPS).all()
    if level:
        qs = qs.filter(level__gte=LEVELS[level.upper()])
    if component:
        qs = qs.filter(Q(logger=component) | Q(logger__startswith=component + "."))
    if before_id:
        qs = qs.filter(id__lt=before_id)
    limit = max(1, min(int(limit), 500))
    rows = list(qs.order_by("-id")[: limit + 1])
    items = [{"id": r.id, "created_at": r.created_at.isoformat(), "level": r.level_name, "component": r.logger,
              "message": r.message, "where": r.where, "exc_type": r.exc_type or None,
              "request_id": r.request_id or None} for r in rows[:limit]]
    handler = logmod.installed()
    return {
        "items": items,
        "next_before_id": rows[limit - 1].id if len(rows) > limit else None,
        "capture": {"enabled": handler is not None, "max_rows": logmod.max_rows(),
                    "dropped": handler.dropped if handler else 0},
    }


# ------------------------------------------------------------------------------------------------ admins
def grant_platform_admin(email: str, *, revoke: bool = False):
    """Set (or clear) `User.is_platform_admin` on an EXISTING user. Audited. Returns (user, changed)."""
    from apps.accounts.services import find_user_by_email, set_platform_admin

    user = find_user_by_email(email)
    if user is None:
        raise LookupError(email)
    changed = set_platform_admin(user, not revoke)
    if changed:
        platform_audit("platform_admin.revoke" if revoke else "platform_admin.grant", target_type="accounts.user",
                       target_id=str(user.pk), metadata={"email": user.email, "via": "manage.py"})
    return user, changed
