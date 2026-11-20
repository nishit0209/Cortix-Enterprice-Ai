"""Conversational BI endpoints (CONTRACT "W3: Conversational BI"). All routes live under /api/v1/orgs/{org}/.

    GET, POST           bi/conversations                       bi.chat (+ dataset ACL `query` on create)
    GET, PATCH, DELETE  bi/conversations/{id}                  bi.chat, owner only (others 404)
    GET, POST           bi/conversations/{id}/messages         bi.chat, owner only; POST streams SSE when
                                                               `Accept: text/event-stream`, else 200 Message
    POST                bi/messages/{id}/feedback              bi.chat, owner only  {value: up|down}
    POST                bi/queries                             bi.sql   manual guarded SQL
    GET                 bi/query-history                       own (bi.chat / bi.sql) or all (audit.view)
    GET, POST           saved-queries · GET/PATCH/DELETE /{id} · POST /{id}/run
    POST                exports                                exports.create + dataset ACL `export`
    GET                 insights                               org.view (the caller's own recent answers)
    GET, POST           dashboards · GET/PATCH/DELETE /{id}    dashboards.view / dashboards.manage (owner only)
    POST                dashboards/{id}/widgets                dashboards.manage (owner only)
    PATCH, DELETE       dashboards/{id}/widgets/{widget_id}    dashboards.manage (owner only)
    GET                 dashboards/{id}/widgets/{widget_id}/data   dashboards.view; re-run under the viewer's policy
"""

from __future__ import annotations

from uuid import UUID

from django.db.models import Q
from django.http import HttpResponse, StreamingHttpResponse
from rest_framework import mixins, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts.services import users_by_id
from apps.core import audit
from apps.core.http import content_disposition
from apps.core.pagination import DefaultCursorPagination
from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantAPIView, TenantGenericViewSet

from . import dashboards, ports, services
from .models import BiQueryAudit, Conversation, Message, SavedQuery
from .serializers import (
    AskSerializer,
    ConversationCreateSerializer,
    ConversationSerializer,
    ConversationUpdateSerializer,
    DashboardCreateSerializer,
    DashboardUpdateSerializer,
    ExportSerializer,
    FeedbackSerializer,
    ManualQuerySerializer,
    SavedQuerySerializer,
    SavedQueryUpdateSerializer,
    WidgetCreateSerializer,
    WidgetUpdateSerializer,
    message_payload,
)
from .streaming import event_stream_response

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _api_key_id(request) -> UUID | None:
    return getattr(request, "_nexora_api_key_id", None)


class EventStreamRenderer(BaseRenderer):
    """Lets content negotiation accept `Accept: text/event-stream`; the body itself is a StreamingHttpResponse.
    Error responses are always rendered as JSON problems (see `SseAwareMixin`)."""

    media_type = "text/event-stream"
    format = "sse"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return JSONRenderer().render(data)


class SseAwareMixin:
    def wants_stream(self, request) -> bool:
        return isinstance(getattr(request, "accepted_renderer", None), EventStreamRenderer)

    def finalize_response(self, request, response, *args, **kwargs):
        if isinstance(response, Response) and self.wants_stream(request):
            request.accepted_renderer = JSONRenderer()  # problems/JSON bodies are never sent as event streams
            request.accepted_media_type = "application/json"
        return super().finalize_response(request, response, *args, **kwargs)


# =============================================================================================== conversations
class ConversationPagination(DefaultCursorPagination):
    ordering = ("-created_at", "-id")


class MessagePagination(DefaultCursorPagination):
    ordering = ("created_at", "id")
    page_size = 100


class ConversationViewSet(SseAwareMixin, mixins.ListModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "bi.chat"
    serializer_class = ConversationSerializer
    pagination_class = ConversationPagination
    lookup_value_regex = UUID_RE
    renderer_classes = [JSONRenderer, EventStreamRenderer]

    def get_queryset(self):
        qs = Conversation.objects.filter(user_id=self.request.user.pk)
        dataset_id = self.request.query_params.get("dataset_id")
        if dataset_id and self.action == "list":
            try:
                qs = qs.filter(dataset_id=UUID(dataset_id))
            except ValueError as exc:
                raise ValidationError({"dataset_id": ["Must be a UUID."]}) from exc
        return qs

    def get_throttles(self):
        if self.action == "messages" and self.request.method == "POST":
            self.throttle_scope = "ai"
            return [*super().get_throttles(), ScopedRateThrottle()]
        return super().get_throttles()

    def create(self, request, org):
        ser = ConversationCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        conv = services.create_conversation(user_id=request.user.pk, dataset_id=ser.validated_data["dataset_id"],
                                            title=ser.validated_data.get("title") or "")
        return Response(ConversationSerializer(conv).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, org, pk=None):
        return Response(ConversationSerializer(self.get_object()).data)

    def partial_update(self, request, org, pk=None):
        conv = self.get_object()
        ser = ConversationUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        before = conv.title
        conv.title = ser.validated_data["title"] or services.DEFAULT_TITLE
        conv.save(update_fields=["title", "updated_at"])
        audit.record("bi.conversation.update", target=conv, changes={"title": [before, conv.title]})
        return Response(ConversationSerializer(conv).data)

    def destroy(self, request, org, pk=None):
        conv = self.get_object()
        audit.record("bi.conversation.delete", target=conv, metadata={"dataset_id": str(conv.dataset_id)})
        conv.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def messages(self, request, org, pk=None):
        conv = self.get_object()
        if request.method == "GET":
            paginator = MessagePagination()
            page = paginator.paginate_queryset(Message.objects.filter(conversation=conv), request, view=self)
            return paginator.get_paginated_response([message_payload(m) for m in page])
        ser = AskSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        # Authorize before anything is written or streamed (404 if the dataset is no longer visible).
        services.ensure_dataset_visible(request.user.pk, conv.dataset_id, privilege="query")
        turn = services.start_turn(conv, ser.validated_data["question"], user_id=request.user.pk,
                                   api_key_id=_api_key_id(request), tenant=request.tenant)
        if self.wants_stream(request):
            # The response is returned now; the pipeline runs while streaming, OUTSIDE this request's
            # transaction (which commits the two new messages when the view returns).
            return event_stream_response(request, lambda: services.ask(turn))
        services.run_to_completion(turn)
        return Response(message_payload(Message.objects.get(id=turn.message_id)))


class MessageFeedbackView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "bi.chat"

    def post(self, request, org, pk):
        ser = FeedbackSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        message = (Message.objects.select_related("conversation")
                   .filter(id=pk, role="assistant", conversation__user_id=request.user.pk).first())
        if message is None:
            raise NotFound()
        return Response(services.apply_feedback(message, ser.validated_data["value"], user_id=request.user.pk))


# =============================================================================================== manual SQL
class ManualQueryView(TenantAPIView):
    """POST {dataset_id, sql} -> {query_audit_id, executed_sql, columns, rows, row_count, truncated, chart}.
    Rejections: 422 problem with `code` = guard reject code and `query_audit_id` (the audit row is kept)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "bi.sql"
    rollback_on_error = False  # the audit row of a rejected/failed query must survive the error response

    def post(self, request, org):
        ser = ManualQuerySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        body = services.run_query(org_id=request.tenant.org_id, user_id=request.user.pk,
                                  api_key_id=_api_key_id(request), dataset_id=ser.validated_data["dataset_id"],
                                  sql=ser.validated_data["sql"], purpose="manual")
        return Response(body)


class QueryHistoryPagination(DefaultCursorPagination):
    ordering = ("-occurred_at", "-id")


class QueryHistoryView(TenantAPIView):
    """Rows of the append-only BI query audit. `audit.view` sees every user; otherwise only your own."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    FILTERS = ("dataset_id", "verdict", "source", "purpose", "user_id", "conversation_id")

    def get(self, request, org):
        tenant = request.tenant
        if not any(tenant.has_perm(p) for p in ("audit.view", "bi.chat", "bi.sql")):
            raise PermissionDenied()
        qs = BiQueryAudit.objects.all()
        if not tenant.has_perm("audit.view"):
            qs = qs.filter(user_id=request.user.pk)
        for name in self.FILTERS:
            value = request.query_params.get(name)
            if not value:
                continue
            if name.endswith("_id"):
                try:
                    value = UUID(value)
                except ValueError as exc:
                    raise ValidationError({name: ["Must be a UUID."]}) from exc
            qs = qs.filter(**{name: value})
        paginator = QueryHistoryPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        users = users_by_id(r.user_id for r in page if r.user_id)
        return paginator.get_paginated_response([_history_item(r, users) for r in page])


def _history_item(r: BiQueryAudit, users: dict) -> dict:
    user = users.get(str(r.user_id)) if r.user_id else None
    return {
        "id": str(r.id),
        "occurred_at": r.occurred_at,
        "user_id": str(r.user_id) if r.user_id else None,
        "user_email": getattr(user, "email", None),
        "api_key_id": str(r.api_key_id) if r.api_key_id else None,
        "dataset": {"id": str(r.dataset_id), "name": r.dataset_name},
        "dataset_id": str(r.dataset_id),
        "conversation_id": str(r.conversation_id) if r.conversation_id else None,
        "message_id": str(r.message_id) if r.message_id else None,
        "purpose": r.purpose,
        "question": r.question,
        "source": r.source,
        "verdict": r.verdict,
        "reject_code": r.reject_code or None,
        "raw_sql": r.raw_sql,
        "executed_sql": r.executed_sql or None,
        "row_count": r.row_count,
        "truncated": r.truncated,
        "duration_ms": r.duration_ms,
        "policy_fingerprint": r.policy_fingerprint,
    }


# =============================================================================================== saved queries
class SavedQueryPagination(DefaultCursorPagination):
    ordering = ("-created_at", "-id")


class SavedQueryViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    """Own + shared saved queries. Only the owner can change or delete one. SQL is validated by the guard on
    save (under the owner's policy) and re-validated under the RUNNER's policy on every run.

    Permission rule (W4, one rule for every saved-query write): reading and running need `dashboards.view`;
    creating, changing and deleting need `dashboards.manage`, exactly like pinning an answer to a dashboard (which
    also creates a SavedQuery). `dashboards.create` is a legacy codename that no route checks any more."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"list": "dashboards.view", "retrieve": "dashboards.view", "run": "dashboards.view",
                            "*": "dashboards.manage"}
    serializer_class = SavedQuerySerializer
    pagination_class = SavedQueryPagination
    lookup_value_regex = UUID_RE
    rollback_on_error = False  # a rejected `run` keeps its audit row

    def get_queryset(self):
        uid = self.request.user.pk
        qs = SavedQuery.objects.filter(Q(owner_id=uid) | Q(is_shared=True))
        if self.action == "list":
            dataset_id = self.request.query_params.get("dataset_id")
            if dataset_id:
                try:
                    qs = qs.filter(dataset_id=UUID(dataset_id))
                except ValueError as exc:
                    raise ValidationError({"dataset_id": ["Must be a UUID."]}) from exc
        return qs

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        visible = ports.visible_dataset_ids(request.user.pk, {q.dataset_id for q in page}, "read") if page else set()
        items = [q for q in page if q.owner_id == request.user.pk or str(q.dataset_id) in visible]
        return self.get_paginated_response(SavedQuerySerializer(items, many=True).data)

    def _visible(self, pk) -> SavedQuery:
        obj = self.get_queryset().filter(id=pk).first()
        if obj is None:
            raise NotFound()
        if obj.owner_id != self.request.user.pk:
            if not ports.check_dataset_access(self.request.user.pk, obj.dataset_id, "read"):
                raise NotFound()
        return obj

    def _owned(self, pk) -> SavedQuery:
        obj = self._visible(pk)
        if obj.owner_id != self.request.user.pk:
            raise PermissionDenied("Only the owner can change this saved query.")
        return obj

    def _validate_sql(self, request, dataset_id, sql, question) -> None:
        services.check_sql(org_id=request.tenant.org_id, user_id=request.user.pk, api_key_id=_api_key_id(request),
                           dataset_id=dataset_id, sql=sql, purpose="saved_query", question=question or "")

    def create(self, request, org):
        ser = SavedQuerySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if SavedQuery.objects.filter(owner_id=request.user.pk, name=data["name"]).exists():
            raise ValidationError({"name": ["You already have a saved query with this name."]})
        self._validate_sql(request, data["dataset_id"], data["sql"], data.get("question"))
        obj = SavedQuery.objects.create(owner_id=request.user.pk, **data)
        audit.record("bi.saved_query.create", target=obj,
                     metadata={"name": obj.name, "dataset_id": str(obj.dataset_id), "is_shared": obj.is_shared})
        return Response(SavedQuerySerializer(obj).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, org, pk=None):
        return Response(SavedQuerySerializer(self._visible(pk)).data)

    def partial_update(self, request, org, pk=None):
        obj = self._owned(pk)
        ser = SavedQueryUpdateSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if "name" in data and data["name"] != obj.name and SavedQuery.objects.filter(
                owner_id=request.user.pk, name=data["name"]).exists():
            raise ValidationError({"name": ["You already have a saved query with this name."]})
        if "sql" in data:
            self._validate_sql(request, obj.dataset_id, data["sql"], data.get("question", obj.question))
        changes = {k: [getattr(obj, k), v] for k, v in data.items() if getattr(obj, k) != v and k != "sql"}
        for k, v in data.items():
            setattr(obj, k, v)
        obj.save()
        audit.record("bi.saved_query.update", target=obj, changes=changes or None,
                     metadata={"sql_changed": "sql" in data})
        return Response(SavedQuerySerializer(obj).data)

    def destroy(self, request, org, pk=None):
        obj = self._owned(pk)
        audit.record("bi.saved_query.delete", target=obj, metadata={"name": obj.name})
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def run(self, request, org, pk=None):
        obj = self._visible(pk)
        body = services.run_query(org_id=request.tenant.org_id, user_id=request.user.pk,
                                  api_key_id=_api_key_id(request), dataset_id=obj.dataset_id, sql=obj.sql,
                                  purpose="saved_query", question=obj.question, chart_hint=obj.chart)
        return Response(body)


# =============================================================================================== exports
class ExportView(TenantAPIView):
    """POST {message_id, format: csv|xlsx} -> the file (attachment). Re-executes the audited query through the
    guard under the caller's export policy; formula-injection cells are escaped."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "exports.create"
    rollback_on_error = False  # keep the audit row of a rejected re-execution

    def post(self, request, org):
        ser = ExportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        export = services.export_message(org_id=request.tenant.org_id, user_id=request.user.pk,
                                         api_key_id=_api_key_id(request),
                                         message_id=ser.validated_data["message_id"],
                                         fmt=ser.validated_data["format"])
        if isinstance(export.body, bytes | bytearray):
            resp = HttpResponse(bytes(export.body), content_type=export.content_type)
        else:
            resp = StreamingHttpResponse(export.body, content_type=export.content_type)
        resp["Content-Disposition"] = content_disposition(export.filename)
        resp["X-Content-Type-Options"] = "nosniff"
        resp["Cache-Control"] = "no-store"
        resp["X-Row-Count"] = str(export.row_count)
        resp["X-Query-Audit-Id"] = str(export.query_audit_id)
        return resp


# =============================================================================================== insights
class InsightsView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "org.view"

    def get(self, request, org):
        try:
            limit = max(1, min(int(request.query_params.get("limit", 12)), 50))
        except ValueError as exc:
            raise ValidationError({"limit": ["Must be an integer."]}) from exc
        items = services.dashboard_insights(request.user.pk, limit=limit)
        return Response({"items": items, "next_cursor": None, "previous_cursor": None})


# =============================================================================================== dashboards
class DashboardPagination(DefaultCursorPagination):
    ordering = ("-updated_at", "-id")


class DashboardListView(TenantAPIView):
    """GET /dashboards (own + shared, dashboards.view) · POST /dashboards {title, description?, is_default?,
    is_shared?} (dashboards.manage)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "dashboards.view", "post": "dashboards.manage"}

    def get(self, request, org):
        qs = dashboards.with_widget_counts(dashboards.visible_dashboards(request.user.pk))
        paginator = DashboardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response([dashboards.dashboard_summary(d) for d in page])

    def post(self, request, org):
        ser = DashboardCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = dashboards.create_dashboard(user_id=request.user.pk, request=request, **ser.validated_data)
        return Response(dashboards.dashboard_payload(d), status=status.HTTP_201_CREATED)


class DashboardDetailView(TenantAPIView):
    """GET (visible) · PATCH {title?, description?, is_default?, is_shared?, layout?: [{id,x,y,w,h}]} · DELETE
    (owner only; others 404 when the dashboard is not shared with them, else 403)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "dashboards.view", "*": "dashboards.manage"}

    def get(self, request, org, pk):
        return Response(dashboards.dashboard_payload(dashboards.get_visible(request.user.pk, pk)))

    def patch(self, request, org, pk):
        d = dashboards.get_owned(request.user.pk, pk)
        ser = DashboardUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        dashboards.update_dashboard(d, ser.validated_data, request=request)
        return Response(dashboards.dashboard_payload(d))

    def delete(self, request, org, pk):
        dashboards.delete_dashboard(dashboards.get_owned(request.user.pk, pk), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DashboardWidgetListView(TenantAPIView):
    """POST /dashboards/{id}/widgets {title, kind, saved_query_id? | message_id?, chart?, layout?, text?}.
    `message_id` pins an answered Ask-AI message (a SavedQuery is created from its audited SQL)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "dashboards.manage"

    def post(self, request, org, pk):
        d = dashboards.get_owned(request.user.pk, pk)
        ser = WidgetCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        widget = dashboards.add_widget(d, org_id=request.tenant.org_id, user_id=request.user.pk,
                                       api_key_id=_api_key_id(request), request=request, **ser.validated_data)
        return Response(dashboards.widget_payload(widget), status=status.HTTP_201_CREATED)


class DashboardWidgetDetailView(TenantAPIView):
    """PATCH / DELETE /dashboards/{id}/widgets/{widget_id} (owner of the dashboard only)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "dashboards.manage"

    def patch(self, request, org, pk, widget_id):
        widget = dashboards.get_widget(dashboards.get_owned(request.user.pk, pk), widget_id)
        ser = WidgetUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        dashboards.update_widget(widget, ser.validated_data, user_id=request.user.pk, request=request)
        return Response(dashboards.widget_payload(widget))

    def delete(self, request, org, pk, widget_id):
        widget = dashboards.get_widget(dashboards.get_owned(request.user.pk, pk), widget_id)
        dashboards.delete_widget(widget, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DashboardWidgetDataView(TenantAPIView):
    """GET /dashboards/{id}/widgets/{widget_id}/data -> {columns, rows, row_count, truncated, chart, refreshed_at,
    query_audit_id}. Re-executed now under the VIEWER's policy (never stored rows); audited (source dashboard)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "dashboards.view"
    rollback_on_error = False  # the audit row of a rejected refresh must survive the 4xx

    def get(self, request, org, pk, widget_id):
        widget = dashboards.get_widget(dashboards.get_visible(request.user.pk, pk), widget_id)
        body = dashboards.widget_data(widget, org_id=request.tenant.org_id, user_id=request.user.pk,
                                      api_key_id=_api_key_id(request))
        resp = Response(body)
        resp["Cache-Control"] = "no-store"
        return resp
