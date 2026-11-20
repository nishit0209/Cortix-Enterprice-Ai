from django.urls import path
from rest_framework.routers import SimpleRouter

from .api import (
    AuditEventViewSet,
    JobViewSet,
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
)
from .sse import job_events

router = SimpleRouter(trailing_slash=False)
router.register("jobs", JobViewSet, basename="job")
router.register("audit-events", AuditEventViewSet, basename="audit-event")

urlpatterns = [
    path("jobs/<uuid:job_id>/events", job_events, name="job-events"),
    path("notifications", NotificationListView.as_view(), name="notification-list"),
    path("notifications/read-all", NotificationReadAllView.as_view(), name="notification-read-all"),
    path("notifications/<uuid:pk>/read", NotificationReadView.as_view(), name="notification-read"),
    *router.urls,
]
