from django.urls import path
from rest_framework.routers import SimpleRouter

from .api import AuditEventViewSet, JobViewSet
from .sse import job_events

router = SimpleRouter(trailing_slash=False)
router.register("jobs", JobViewSet, basename="job")
router.register("audit-events", AuditEventViewSet, basename="audit-event")

urlpatterns = [
    path("jobs/<uuid:job_id>/events", job_events, name="job-events"),
    *router.urls,
]
