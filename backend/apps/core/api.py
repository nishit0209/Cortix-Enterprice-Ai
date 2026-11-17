from rest_framework import mixins
from rest_framework.permissions import IsAuthenticated

from .models import AuditEvent, Job
from .permissions import HasOrgPermission
from .serializers import AuditEventSerializer, JobSerializer
from .tenancy.views import TenantGenericViewSet


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
