"""Org usage report: `GET /orgs/{org}/usage?from=YYYY-MM-DD&to=YYYY-MM-DD&metric=` (`org.view`, Phase 5).

Every number is an aggregate computed from the DB inside the caller's tenant context (RLS: only the active org).
No data rows, no values: counts, sums and per-day series only.

    queries        BI query audit: total, by verdict / purpose, per day, per user (per-user only with members.view)
    active_users   distinct users with audited activity (audit events, BI queries, data access) per day + members
    datasets       live datasets, by status, created in range (per day)
    storage        upload bytes + warehouse table bytes (datasets.services.data_stats) + document bytes
    ai             Gemini requests / tokens / blocked / errors per day (apps.ai.services.usage_summary)
    pipeline_runs  pipeline runs started in range, by status, per day
    documents      documents, by status, created in range (per day)

A metric whose source app is missing or fails is reported as `{"available": false}`; the others still answer.
Counts without a range service in their owning app (pipeline runs, documents, datasets created per day) are
read-only aggregates over that app's model, looked up by name (requested as services in the W4 handoff).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from django.apps import apps as django_apps
from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import BiQueryAudit

log = logging.getLogger(__name__)

METRICS = ("queries", "active_users", "datasets", "storage", "ai", "pipeline_runs", "documents")
DEFAULT_DAYS = 30
MAX_DAYS = 366
TOP_USERS = 20


class UsageRangeError(ValueError):
    pass


def parse_range(raw_from: str | None, raw_to: str | None, *, today: date | None = None) -> tuple[date, date]:
    today = today or timezone.now().astimezone(UTC).date()
    try:
        end = date.fromisoformat(raw_to) if raw_to else today
        start = date.fromisoformat(raw_from) if raw_from else end - timedelta(days=DEFAULT_DAYS - 1)
    except ValueError as exc:
        raise UsageRangeError("from/to must be dates (YYYY-MM-DD).") from exc
    if start > end:
        raise UsageRangeError("from must not be after to.")
    if (end - start).days + 1 > MAX_DAYS:
        raise UsageRangeError(f"The range may cover at most {MAX_DAYS} days.")
    return start, end


def bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00 UTC, end+1 00:00 UTC)"""
    return (datetime.combine(start, time.min, tzinfo=UTC), datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC))


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def per_day(qs, field: str, start: date, end: date) -> list[dict]:
    """Zero-filled [{day, count}] of `qs` grouped by the UTC day of `field`."""
    counts = {row["day"]: row["n"] for row in qs.annotate(day=TruncDate(field, tzinfo=UTC)).values("day")
              .annotate(n=Count("id")).order_by("day")}
    return [{"day": d.isoformat(), "count": int(counts.get(d, 0))} for d in days_between(start, end)]


def _model(label: str):
    try:
        return django_apps.get_model(label)
    except LookupError:
        return None


# ------------------------------------------------------------------------------------------------ metrics
def queries(start, end, *, include_users: bool) -> dict:
    lo, hi = bounds(start, end)
    qs = BiQueryAudit.objects.filter(occurred_at__gte=lo, occurred_at__lt=hi)
    out = {
        "total": qs.count(),
        "by_verdict": {r["verdict"]: r["n"] for r in qs.values("verdict").annotate(n=Count("id")).order_by()},
        "by_purpose": {r["purpose"]: r["n"] for r in qs.values("purpose").annotate(n=Count("id")).order_by()},
        "per_day": per_day(qs, "occurred_at", start, end),
    }
    if include_users:
        from apps.accounts.services import users_by_id

        top = list(qs.exclude(user_id=None).values("user_id").annotate(n=Count("id")).order_by("-n", "user_id")
                   [:TOP_USERS])
        users = users_by_id(r["user_id"] for r in top)
        out["per_user"] = [{"user_id": str(r["user_id"]), "email": getattr(users.get(str(r["user_id"])), "email", None),
                            "count": r["n"]} for r in top]
    return out


def active_users(start, end) -> dict:
    from apps.core.models import AuditEvent
    from apps.governance.models import AccessHistory
    from apps.orgs.models import Membership

    lo, hi = bounds(start, end)
    pairs: set[tuple[date, str]] = set()
    sources = (
        (AuditEvent.objects.filter(occurred_at__gte=lo, occurred_at__lt=hi, actor_type="user")
         .exclude(actor_id=None), "occurred_at", "actor_id"),
        (BiQueryAudit.objects.filter(occurred_at__gte=lo, occurred_at__lt=hi).exclude(user_id=None),
         "occurred_at", "user_id"),
        (AccessHistory.objects.filter(occurred_at__gte=lo, occurred_at__lt=hi).exclude(user_id=None),
         "occurred_at", "user_id"),
    )
    for qs, field, user_field in sources:
        for day, uid in qs.annotate(day=TruncDate(field, tzinfo=UTC)).values_list("day", user_field).distinct():
            pairs.add((day, str(uid)))
    by_day: dict[date, set[str]] = defaultdict(set)
    for day, uid in pairs:
        by_day[day].add(uid)
    return {
        "distinct": len({uid for _, uid in pairs}),
        "members_active": Membership.objects.filter(status="active").count(),
        "per_day": [{"day": d.isoformat(), "count": len(by_day.get(d, ()))} for d in days_between(start, end)],
    }


def datasets(start, end) -> dict:
    Dataset = _model("datasets.Dataset")
    if Dataset is None:
        return {"available": False}
    lo, hi = bounds(start, end)
    live = Dataset.objects.exclude(status="deleted")
    created = Dataset.objects.filter(created_at__gte=lo, created_at__lt=hi)
    return {
        "total": live.count(),
        "by_status": {r["status"]: r["n"] for r in live.values("status").annotate(n=Count("id")).order_by()},
        "created": created.count(),
        "per_day": per_day(created, "created_at", start, end),
    }


def storage(start, end) -> dict:
    from apps.datasets import services as datasets_services

    stats = datasets_services.data_stats()
    dataset_bytes = int(stats.get("storage_bytes") or 0)
    Upload = _model("datasets.Upload")
    upload_bytes = int((Upload.objects.filter(status="ready").aggregate(b=Sum("size_bytes"))["b"] or 0)
                       if Upload is not None else 0)
    Document = _model("documents.Document")
    document_bytes = int((Document.objects.aggregate(b=Sum("size_bytes"))["b"] or 0) if Document is not None else 0)
    return {
        "total_bytes": dataset_bytes + document_bytes,
        "uploads_bytes": upload_bytes,
        "warehouse_bytes": max(0, dataset_bytes - upload_bytes),
        "documents_bytes": document_bytes,
        "as_of": timezone.now().isoformat(),
    }


def ai(start, end, *, today: date | None = None) -> dict:
    from apps.ai import services as ai_services

    today = today or timezone.now().astimezone(UTC).date()
    span = min(90, max(1, (today - start).days + 2))  # usage_summary looks back from today (Pacific), at most 90 d
    summary = ai_services.usage_summary(days=span)
    totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "blocked": 0, "errors": 0}
    daily: dict[str, dict] = {}
    for row in summary.get("daily", []):
        day = row.get("day")
        if not day or not (start.isoformat() <= day <= end.isoformat()):
            continue
        slot = daily.setdefault(day, {"requests": 0, "tokens": 0})
        slot["requests"] += int(row.get("requests") or 0)
        slot["tokens"] += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
        for key in totals:
            totals[key] += int(row.get(key) or 0)
    return {
        **totals,
        "tokens": totals["input_tokens"] + totals["output_tokens"],
        "per_day": [{"day": d.isoformat(), **daily.get(d.isoformat(), {"requests": 0, "tokens": 0})}
                    for d in days_between(start, end)],
        "covered_from": summary.get("range", {}).get("from"),
        "timezone": summary.get("range", {}).get("timezone"),
    }


def pipeline_runs(start, end) -> dict:
    PipelineRun = _model("pipelines.PipelineRun")
    if PipelineRun is None:
        return {"available": False}
    lo, hi = bounds(start, end)
    qs = PipelineRun.objects.filter(created_at__gte=lo, created_at__lt=hi)
    return {
        "total": qs.count(),
        "by_status": {r["status"]: r["n"] for r in qs.values("status").annotate(n=Count("id")).order_by()},
        "per_day": per_day(qs, "created_at", start, end),
    }


def documents(start, end) -> dict:
    Document = _model("documents.Document")
    if Document is None:
        return {"available": False}
    lo, hi = bounds(start, end)
    created = Document.objects.filter(created_at__gte=lo, created_at__lt=hi)
    return {
        "total": Document.objects.count(),
        "by_status": {r["status"]: r["n"] for r in Document.objects.values("status").annotate(n=Count("id"))
                      .order_by()},
        "created": created.count(),
        "per_day": per_day(created, "created_at", start, end),
    }


def _safe(name: str, fn: Callable[[], dict]) -> dict:
    try:
        with transaction.atomic():  # savepoint: a failing metric never poisons the request transaction
            return fn()
    except Exception:  # noqa: BLE001 - one unavailable source never breaks the report
        log.warning("usage: metric %s failed", name, exc_info=True)
        return {"available": False}


def usage_report(start: date, end: date, *, metric: str | None = None, include_users: bool = False) -> dict:
    """Inside a tenant context. `metric` limits the report to one metric (validated by the caller)."""
    builders: dict[str, Callable[[], dict]] = {
        "queries": lambda: queries(start, end, include_users=include_users),
        "active_users": lambda: active_users(start, end),
        "datasets": lambda: datasets(start, end),
        "storage": lambda: storage(start, end),
        "ai": lambda: ai(start, end),
        "pipeline_runs": lambda: pipeline_runs(start, end),
        "documents": lambda: documents(start, end),
    }
    names = [metric] if metric else list(METRICS)
    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(), "days": (end - start).days + 1, "timezone": "UTC"},
        "metrics": {name: _safe(name, builders[name]) for name in names},
        "generated_at": timezone.now().isoformat(),
    }
