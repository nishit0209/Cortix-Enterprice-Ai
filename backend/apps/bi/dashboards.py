"""Dashboards and widgets (CONTRACT "W3 additions / Dashboards"). Re-exported by `apps.bi.services`.

Visibility: a dashboard is visible to its owner, and to every member with `dashboards.view` once it is shared.
Only the owner changes it (others: 404 when invisible, 403 when visible). Widget DATA is never stored: every
refresh re-executes the widget's SavedQuery through `sqlguard.validate` + `execute` under the VIEWER's effective
policy and writes a `BiQueryAudit` row (purpose and source `dashboard`). A widget whose dataset the viewer cannot
query answers 404, exactly like the dataset itself.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core import audit
from apps.core.exceptions import Conflict

from . import ports
from .models import (
    GRID_COLUMNS,
    BiQueryAudit,
    Dashboard,
    DashboardWidget,
    Message,
    SavedQuery,
    default_layout,
)
from .results import CHART_TYPES

MAX_WIDGETS = 50
MAX_GRID_ROWS = 1000
MAX_TILE_HEIGHT = 24
DATA_KINDS = frozenset({"chart", "table", "kpi"})
WIDGET_KIND_NAMES = frozenset({"chart", "table", "kpi", "text"})


# ================================================================================================ validation
def clean_layout(value: Any, *, field: str = "layout") -> dict:
    """{x, y, w, h} integers on a GRID_COLUMNS-wide grid (x + w <= GRID_COLUMNS)."""
    if not isinstance(value, dict):
        raise ValidationError({field: ["Must be an object {x, y, w, h}."]})
    out = {}
    for key in ("x", "y", "w", "h"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValidationError({field: [f"'{key}' must be an integer."]})
        out[key] = raw
    if not (0 <= out["x"] < GRID_COLUMNS and 1 <= out["w"] <= GRID_COLUMNS and out["x"] + out["w"] <= GRID_COLUMNS):
        raise ValidationError({field: [f"x and w must fit a {GRID_COLUMNS}-column grid (x + w <= {GRID_COLUMNS})."]})
    if not (0 <= out["y"] <= MAX_GRID_ROWS and 1 <= out["h"] <= MAX_TILE_HEIGHT):
        raise ValidationError({field: [f"y must be 0..{MAX_GRID_ROWS} and h 1..{MAX_TILE_HEIGHT}."]})
    return out


def clean_chart(value: Any, *, field: str = "chart") -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") not in CHART_TYPES:
        raise ValidationError({field: [f"Must be an object with type in {sorted(CHART_TYPES)}."]})
    y = value.get("y") or []
    if isinstance(y, str):
        y = [y]
    if not isinstance(y, list) or len(y) > 10 or not all(isinstance(c, str) and len(c) <= 128 for c in y):
        raise ValidationError({field: ["'y' must be a list of at most 10 column names."]})
    out = {"type": value["type"], "x": value.get("x"), "y": list(y), "series": value.get("series")}
    for key in ("x", "series"):
        if out[key] is not None and (not isinstance(out[key], str) or len(out[key]) > 128):
            raise ValidationError({field: [f"'{key}' must be a column name."]})
    return out


# ================================================================================================ lookup
def visible_dashboards(user_id: UUID):
    return Dashboard.objects.filter(Q(owner_id=user_id) | Q(is_shared=True))


def get_visible(user_id: UUID, pk) -> Dashboard:
    obj = visible_dashboards(user_id).filter(id=pk).first()
    if obj is None:
        raise NotFound()
    return obj


def get_owned(user_id: UUID, pk) -> Dashboard:
    obj = get_visible(user_id, pk)
    if obj.owner_id != user_id:
        raise PermissionDenied("Only the owner can change this dashboard.")
    return obj


def get_widget(dashboard: Dashboard, wid) -> DashboardWidget:
    widget = DashboardWidget.objects.select_related("saved_query").filter(id=wid, dashboard=dashboard).first()
    if widget is None:
        raise NotFound()
    return widget


def visible_saved_query(user_id: UUID, saved_query_id) -> SavedQuery:
    """Own, or shared AND on a dataset the caller can read (else 404, never revealing it exists)."""
    sq = SavedQuery.objects.filter(Q(owner_id=user_id) | Q(is_shared=True), id=saved_query_id).first()
    if sq is None or (sq.owner_id != user_id and not ports.check_dataset_access(user_id, sq.dataset_id, "read")):
        raise ValidationError({"saved_query_id": ["Saved query not found."]})
    return sq


# ================================================================================================ payloads
def widget_payload(w: DashboardWidget) -> dict:
    return {
        "id": str(w.id),
        "title": w.title,
        "kind": w.kind,
        "saved_query_id": str(w.saved_query_id) if w.saved_query_id else None,
        "dataset_id": str(w.saved_query.dataset_id) if w.saved_query_id and w.saved_query else None,
        "chart": w.chart,
        "layout": w.layout or default_layout(),
        "text": w.text or None,
        "created_at": w.created_at,
        "updated_at": w.updated_at,
    }


def dashboard_summary(d: Dashboard, widget_count: int | None = None) -> dict:
    if widget_count is None:
        widget_count = getattr(d, "widget_count", None)
    if widget_count is None:
        widget_count = DashboardWidget.objects.filter(dashboard=d).count()
    return {
        "id": str(d.id),
        "title": d.title,
        "description": d.description,
        "is_default": d.is_default,
        "is_shared": d.is_shared,
        "owner_id": str(d.owner_id),
        "widget_count": widget_count,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


def dashboard_payload(d: Dashboard) -> dict:
    widgets = list(DashboardWidget.objects.select_related("saved_query").filter(dashboard=d)
                   .order_by("created_at", "id"))
    widgets.sort(key=lambda w: ((w.layout or {}).get("y", 0), (w.layout or {}).get("x", 0)))
    return {**dashboard_summary(d, len(widgets)), "widgets": [widget_payload(w) for w in widgets]}


def with_widget_counts(qs):
    return qs.annotate(widget_count=Count("widgets"))


# ================================================================================================ dashboards
def _touch(dashboard: Dashboard) -> None:
    now = timezone.now()
    Dashboard.objects.filter(id=dashboard.id).update(updated_at=now)
    dashboard.updated_at = now


def _make_default(dashboard: Dashboard) -> None:
    Dashboard.objects.filter(owner_id=dashboard.owner_id, is_default=True).exclude(id=dashboard.id).update(
        is_default=False, updated_at=timezone.now())
    dashboard.is_default = True


def create_dashboard(*, user_id: UUID, title: str, description: str = "", is_default: bool = False,
                     is_shared: bool = False, request=None) -> Dashboard:
    title = (title or "").strip()
    if not title:
        raise ValidationError({"title": ["This field may not be blank."]})
    dashboard = Dashboard(owner_id=user_id, title=title[:200], description=description or "", is_shared=is_shared)
    if is_default or not Dashboard.objects.filter(owner_id=user_id, is_default=True).exists():
        _make_default(dashboard)  # the first dashboard of a user becomes their default
    dashboard.save()
    audit.record("bi.dashboard.create", target=dashboard, request=request,
                 metadata={"title": dashboard.title, "is_default": dashboard.is_default, "is_shared": is_shared})
    return dashboard


def update_dashboard(dashboard: Dashboard, data: dict, *, request=None) -> Dashboard:
    changes: dict[str, list] = {}
    for key in ("title", "description", "is_shared"):
        if key in data and data[key] != getattr(dashboard, key):
            if key == "title" and not str(data[key]).strip():
                raise ValidationError({"title": ["This field may not be blank."]})
            changes[key] = [getattr(dashboard, key), data[key]]
            setattr(dashboard, key, data[key])
    if "is_default" in data and bool(data["is_default"]) != dashboard.is_default:
        changes["is_default"] = [dashboard.is_default, bool(data["is_default"])]
        if data["is_default"]:
            _make_default(dashboard)
        else:
            dashboard.is_default = False
    moved = 0
    if data.get("layout") is not None:
        moved = _apply_positions(dashboard, data["layout"])
    if changes or moved:
        dashboard.save()
        audit.record("bi.dashboard.update", target=dashboard, request=request, changes=changes or None,
                     metadata={"widgets_moved": moved})
    return dashboard


def _apply_positions(dashboard: Dashboard, items: Any) -> int:
    if not isinstance(items, list) or len(items) > MAX_WIDGETS:
        raise ValidationError({"layout": [f"Must be a list of at most {MAX_WIDGETS} {{id, x, y, w, h}} items."]})
    widgets = {str(w.id): w for w in DashboardWidget.objects.filter(dashboard=dashboard)}
    updates = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or str(item.get("id")) not in widgets:
            raise ValidationError({f"layout.{i}.id": ["Unknown widget of this dashboard."]})
        widget = widgets[str(item["id"])]
        widget.layout = clean_layout(item, field=f"layout.{i}")
        updates.append(widget)
    now = timezone.now()
    for widget in updates:
        widget.updated_at = now
    DashboardWidget.objects.bulk_update(updates, ["layout", "updated_at"])
    return len(updates)


def delete_dashboard(dashboard: Dashboard, *, request=None) -> None:
    audit.record("bi.dashboard.delete", target=dashboard, request=request,
                 metadata={"title": dashboard.title,
                           "widgets": DashboardWidget.objects.filter(dashboard=dashboard).count()})
    dashboard.delete()


# ================================================================================================ widgets
def _next_free_y(dashboard: Dashboard) -> int:
    bottom = 0
    for layout in DashboardWidget.objects.filter(dashboard=dashboard).values_list("layout", flat=True):
        layout = layout or {}
        bottom = max(bottom, int(layout.get("y", 0) or 0) + int(layout.get("h", 0) or 0))
    return min(bottom, MAX_GRID_ROWS)


def _unique_query_name(user_id: UUID, base: str) -> str:
    base = (base or "Pinned answer").strip()[:180] or "Pinned answer"
    taken = set(SavedQuery.objects.filter(owner_id=user_id, name__startswith=base).values_list("name", flat=True))
    if base not in taken:
        return base
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def pin_message(*, org_id: UUID, user_id: UUID, api_key_id: UUID | None, message_id, name: str,
                request=None) -> SavedQuery:
    """Turn an answered Ask-AI message into a SavedQuery. The SQL comes from the message's AUDITED query (the
    `BiQueryAudit` row with verdict ok, never the message's stored rows) and is re-validated by the guard under
    the caller's current policy before it is saved."""
    from .services import check_sql

    message = (Message.objects.select_related("conversation")
               .filter(id=message_id, role="assistant", conversation__user_id=user_id).first())
    if message is None:
        raise ValidationError({"message_id": ["Message not found."]})
    if message.status != "ok" or not message.query_audit_id:
        raise Conflict("This answer has no query result to pin.", code="pin_not_available")
    source = BiQueryAudit.objects.filter(id=message.query_audit_id, verdict="ok").first()
    if source is None or not source.raw_sql:
        raise Conflict("This answer has no query result to pin.", code="pin_not_available")
    dataset_id = message.conversation.dataset_id
    check_sql(org_id=org_id, user_id=user_id, api_key_id=api_key_id, dataset_id=dataset_id, sql=source.raw_sql,
              purpose="saved_query", question=message.question)
    sq = SavedQuery.objects.create(owner_id=user_id, dataset_id=dataset_id, name=_unique_query_name(user_id, name),
                                   question=message.question, sql=source.raw_sql, chart=message.chart)
    audit.record("bi.saved_query.create", target=sq, request=request,
                 metadata={"name": sq.name, "dataset_id": str(dataset_id), "pinned_from_message": str(message.id),
                           "query_audit_id": str(source.id)})
    return sq


def add_widget(dashboard: Dashboard, *, org_id: UUID, user_id: UUID, api_key_id: UUID | None = None, title: str,
               kind: str, saved_query_id=None, message_id=None, chart=None, layout=None, text: str = "",
               request=None) -> DashboardWidget:
    if kind not in WIDGET_KIND_NAMES:
        raise ValidationError({"kind": [f"Must be one of {sorted(WIDGET_KIND_NAMES)}."]})
    title = (title or "").strip()[:200]
    if not title:
        raise ValidationError({"title": ["This field may not be blank."]})
    if DashboardWidget.objects.filter(dashboard=dashboard).count() >= MAX_WIDGETS:
        raise Conflict(f"A dashboard holds at most {MAX_WIDGETS} widgets.", code="too_many_widgets")
    chart = clean_chart(chart)
    sq = None
    if kind == "text":
        if saved_query_id or message_id:
            raise ValidationError({"kind": ["A text widget has no query."]})
        if not (text or "").strip():
            raise ValidationError({"text": ["A text widget needs text."]})
    else:
        if bool(saved_query_id) == bool(message_id):
            raise ValidationError({"saved_query_id": ["Give exactly one of saved_query_id or message_id."]})
        if saved_query_id:
            sq = visible_saved_query(user_id, saved_query_id)
        else:
            sq = pin_message(org_id=org_id, user_id=user_id, api_key_id=api_key_id, message_id=message_id,
                             name=title, request=request)
        if chart is None and sq.chart:
            chart = clean_chart(sq.chart)
        if kind == "kpi" and chart is None:
            chart = {"type": "kpi", "x": None, "y": [], "series": None}
        text = ""
    if layout is None:
        layout = {**default_layout(), "y": _next_free_y(dashboard)}
    widget = DashboardWidget.objects.create(dashboard=dashboard, title=title, kind=kind, saved_query=sq,
                                            chart=chart, layout=clean_layout(layout), text=(text or "")[:20_000])
    _touch(dashboard)
    audit.record("bi.dashboard.widget.create", target=widget, request=request,
                 metadata={"dashboard_id": str(dashboard.id), "kind": kind,
                           "saved_query_id": str(sq.id) if sq else None,
                           "message_id": str(message_id) if message_id else None})
    return widget


def update_widget(widget: DashboardWidget, data: dict, *, user_id: UUID, request=None) -> DashboardWidget:
    changes: dict[str, Any] = {}
    if "title" in data:
        title = str(data["title"] or "").strip()[:200]
        if not title:
            raise ValidationError({"title": ["This field may not be blank."]})
        if title != widget.title:
            changes["title"] = [widget.title, title]
            widget.title = title
    if "kind" in data and data["kind"] != widget.kind:
        if data["kind"] not in WIDGET_KIND_NAMES:
            raise ValidationError({"kind": [f"Must be one of {sorted(WIDGET_KIND_NAMES)}."]})
        changes["kind"] = [widget.kind, data["kind"]]
        widget.kind = data["kind"]
    if "saved_query_id" in data:
        sq = visible_saved_query(user_id, data["saved_query_id"]) if data["saved_query_id"] else None
        if (sq.id if sq else None) != widget.saved_query_id:
            changes["saved_query_id"] = [str(widget.saved_query_id or ""), str(sq.id) if sq else ""]
            widget.saved_query = sq
    if "chart" in data:
        widget.chart = clean_chart(data["chart"])
        changes["chart"] = True
    if "layout" in data:
        widget.layout = clean_layout(data["layout"])
        changes["layout"] = True
    if "text" in data:
        widget.text = str(data["text"] or "")[:20_000]
        changes["text"] = True
    if widget.kind == "text":
        if widget.saved_query_id:
            raise ValidationError({"kind": ["A text widget has no query."]})
        if not widget.text.strip():
            raise ValidationError({"text": ["A text widget needs text."]})
    elif widget.saved_query_id is None:
        raise ValidationError({"saved_query_id": ["A data widget needs a saved query."]})
    if changes:
        widget.save()
        _touch(widget.dashboard)
        audit.record("bi.dashboard.widget.update", target=widget, request=request,
                     changes={k: v for k, v in changes.items() if isinstance(v, list)} or None,
                     metadata={"dashboard_id": str(widget.dashboard_id), "fields": sorted(changes)})
    return widget


def delete_widget(widget: DashboardWidget, *, request=None) -> None:
    audit.record("bi.dashboard.widget.delete", target=widget, request=request,
                 metadata={"dashboard_id": str(widget.dashboard_id), "title": widget.title})
    dashboard = widget.dashboard
    widget.delete()
    _touch(dashboard)


def widget_data(widget: DashboardWidget, *, org_id: UUID, user_id: UUID, api_key_id: UUID | None = None) -> dict:
    """Re-run the widget's saved query NOW, through the guard, under the viewer's policy (audited, source
    `dashboard`). Raises QueryRejected (422, audited) / DatasetNotFound (404) like a manual query."""
    from .services import run_query

    if widget.kind not in DATA_KINDS:
        raise Conflict("This widget has no data.", code="widget_has_no_data")
    sq = widget.saved_query
    if sq is None:
        raise Conflict("The saved query of this widget was deleted.", code="widget_query_missing")
    body = run_query(org_id=org_id, user_id=user_id, api_key_id=api_key_id, dataset_id=sq.dataset_id, sql=sq.sql,
                     purpose="dashboard", source="dashboard", question=sq.question or widget.title,
                     chart_hint=widget.chart or sq.chart)
    if widget.chart:
        body["chart"] = widget.chart
    return {
        "columns": body["columns"],
        "rows": body["rows"],
        "row_count": body["row_count"],
        "truncated": body["truncated"],
        "chart": body["chart"],
        "refreshed_at": timezone.now(),
        "query_audit_id": body["query_audit_id"],
        "dataset_id": str(sq.dataset_id),
    }
