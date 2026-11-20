from uuid import UUID

from rest_framework import mixins
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import audit, notifications
from .models import AuditEvent, Job, Notification
from .pagination import DefaultCursorPagination
from .permissions import HasOrgPermission
from .serializers import AuditEventSerializer, JobSerializer, NotificationSerializer
from .tenancy.views import TenantAPIView, TenantGenericViewSet


class JobViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, TenantGenericViewSet):
    serializer_class = JobSerializer
    permission_classes = [IsAuthenticated, HasOrgPermission]

    def get_queryset(self):
        qs = Job.objects.all()
        if not self.request.tenant.has_perm("jobs.view_all"):
            qs = qs.filter(user_id=self.request.user.pk)
        return qs


class AuditEventViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    serializer_class = AuditEventSerializer
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "audit.view"
    filterset_fields = ["action", "target_type", "target_id", "actor_id"]

    def get_queryset(self):
        return AuditEvent.objects.all()


# ================================================================================================ notifications
class NotificationPagination(DefaultCursorPagination):
    ordering = ("-created_at", "-id")
    unread_count = 0

    def get_paginated_response(self, data):
        resp = super().get_paginated_response(data)
        resp.data["unread_count"] = self.unread_count
        return resp


def _flag(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes"):
        return True
    if lowered in ("0", "false", "no"):
        return False
    raise ValidationError({"unread": ["Must be 1 or 0."]})


class NotificationListView(TenantAPIView):
    """GET /orgs/{org}/notifications?unread=1 — the caller's own notifications, newest first (cursor-paginated).
    The envelope adds `unread_count` (for the bell badge)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]

    def get(self, request, org):
        qs = Notification.objects.filter(user_id=request.user.pk)
        unread = _flag(request.query_params.get("unread"))
        if unread is True:
            qs = qs.filter(read_at__isnull=True)
        elif unread is False:
            qs = qs.filter(read_at__isnull=False)
        paginator = NotificationPagination()
        paginator.unread_count = notifications.unread_count(request.user.pk)
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(NotificationSerializer(page, many=True).data)


class NotificationReadView(TenantAPIView):
    """POST /orgs/{org}/notifications/{id}/read — someone else's notification is 404."""

    permission_classes = [IsAuthenticated, HasOrgPermission]

    def post(self, request, org, pk: UUID):
        if not notifications.mark_read(request.user.pk, pk):
            raise NotFound()
        item = Notification.objects.get(id=pk)
        audit.record("notification.read", target=item, metadata={"kind": item.kind}, request=request)
        return Response({**NotificationSerializer(item).data,
                         "unread_count": notifications.unread_count(request.user.pk)})


class NotificationReadAllView(TenantAPIView):
    """POST /orgs/{org}/notifications/read-all -> {updated, unread_count}."""

    permission_classes = [IsAuthenticated, HasOrgPermission]

    def post(self, request, org):
        updated = notifications.mark_all_read(request.user.pk)
        if updated:
            audit.record("notification.read_all", target_type="core.notification", metadata={"updated": updated},
                         request=request)
        return Response({"updated": updated, "unread_count": notifications.unread_count(request.user.pk)})
