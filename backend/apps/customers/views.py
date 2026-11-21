"""Customer 360 API (API_SPECIFICATION Phase 6). All routes under /api/v1/orgs/{org}/customers.

GET    /customers?search=                      customers.view    golden profiles (cursor envelope)
GET    /customers/{id}                         customers.view    profile + linked records, segments, matches, scores
GET    /customers/{id}/timeline?event_type=    customers.view    events, newest first (cursor envelope)
GET    /customers/{id}/scores                  customers.view    latest + historical scores
GET    /customers/settings                     customers.view    thresholds / weights / survivorship (+ defaults)
PATCH  /customers/settings                     customers.manage
GET    /customers/sources                      customers.view
POST   /customers/sources                      customers.manage  {dataset_id, kind: profile|event, mapping, name?,
                                                                  source_system?, priority?, link_source_id?}
GET    /customers/sources/{id}                 customers.view
DELETE /customers/sources/{id}                 customers.manage
POST   /customers/resolve                      customers.manage  202 {run_id, job_id, status_url, events_url}
GET    /customers/resolution-runs[/{id}]       customers.view
GET    /customers/matches?status=pending_review customers.review
GET    /customers/matches/{id}                 customers.review
POST   /customers/matches/{id}/decision        customers.review  {decision: confirm|reject, note?}
GET    /customers/segments                     customers.manage
POST   /customers/segments                     customers.manage  {name, method: rule, definition} | {method: rfm}
GET    /customers/segments/{id}                customers.manage
DELETE /customers/segments/{id}                customers.manage
GET    /customers/segments/{id}/members        customers.manage  (cursor envelope)
POST   /customers/segments/{id}/refresh        customers.manage
GET    /customers/scoring-runs?score_type=     customers.view    scoring-run history (cursor envelope)
POST   /customers/scoring-runs                 customers.manage  {score_type: churn|clv|next_best_action, model_id?}
                                                                  202 {run_id, job_id, ...}; 409 ml_model_required
                                                                  without a servable model (churn/clv only)
POST   /customers/features/publish             customers.manage  {kind: current|training} 202 publish_frame of
                                                                  non-PII features (training adds churned/future_value)
"""

from __future__ import annotations

from django.db.models import Q
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.pagination import DefaultCursorPagination
from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantAPIView

from . import services
from .models import (
    CustomerEvent,
    CustomerProfile,
    CustomerScore,
    CustomerSegment,
    CustomerSegmentMember,
    CustomerSource,
    IdentityMatch,
    ResolutionRun,
)


def job_links(request, job) -> dict:
    base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{job.id}"
    return {"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"}


def _get(model, pk):
    obj = model.objects.filter(id=pk).first()
    if obj is None:
        raise NotFound()
    return obj


class _Paged(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    ordering = "-created_at"

    def page(self, request, qs, render):
        paginator = DefaultCursorPagination()
        paginator.ordering = self.ordering
        items = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response([render(o) for o in items])


class ProfileListView(_Paged):
    required_permissions = {"get": "customers.view"}

    def get(self, request, **kwargs):
        qs = CustomerProfile.objects.all()
        term = (request.query_params.get("search") or "").strip()
        if term:
            qs = qs.filter(Q(display_name__icontains=term) | Q(email__icontains=term) | Q(phone__icontains=term))
        segment = request.query_params.get("segment_id")
        if segment:
            qs = qs.filter(id__in=CustomerSegmentMember.objects.filter(segment_id=segment).values("profile_id"))
        source = request.query_params.get("source")
        if source:
            qs = qs.filter(sources__contains=[source])
        return self.page(request, qs, services.profile_payload)


class ProfileDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "customers.view"}

    def get(self, request, pk, **kwargs):
        return Response(services.profile_detail(_get(CustomerProfile, pk)))


class ProfileTimelineView(_Paged):
    required_permissions = {"get": "customers.view"}
    ordering = "-occurred_at"

    def get(self, request, pk, **kwargs):
        profile = _get(CustomerProfile, pk)
        qs = CustomerEvent.objects.filter(profile_id=profile.id).select_related("source")
        types = [t for t in (request.query_params.get("event_type") or "").split(",") if t]
        if types:
            qs = qs.filter(event_type__in=types)
        return self.page(request, qs, services.event_payload)


class ProfileScoresView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "customers.view"}

    def get(self, request, pk, **kwargs):
        profile = _get(CustomerProfile, pk)
        history = CustomerScore.objects.filter(profile_id=profile.id).order_by("-scored_at")[:200]
        return Response({"latest": [services.score_payload(s) for s in services.latest_scores(profile)],
                         "history": [services.score_payload(s) for s in history]})


class SettingsView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "customers.view", "patch": "customers.manage"}

    def get(self, request, **kwargs):
        return Response(services.settings_payload(services.get_settings()))

    def patch(self, request, **kwargs):
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": ["A JSON object is required."]})
        return Response(services.settings_payload(services.update_settings(request.data, request=request)))


class SourceListView(_Paged):
    required_permissions = {"get": "customers.view", "post": "customers.manage"}

    def get(self, request, **kwargs):
        qs = CustomerSource.objects.all()
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        return self.page(request, qs, services.source_payload)

    def post(self, request, **kwargs):
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": ["A JSON object is required."]})
        source = services.create_source(request.data, user_id=request.user.pk, request=request)
        return Response(services.source_payload(source), status=status.HTTP_201_CREATED)


class SourceDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "customers.view", "delete": "customers.manage"}

    def get(self, request, pk, **kwargs):
        return Response(services.source_payload(_get(CustomerSource, pk)))

    def delete(self, request, pk, **kwargs):
        services.delete_source(_get(CustomerSource, pk), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ResolveView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"post": "customers.manage"}

    def post(self, request, **kwargs):
        run, job = services.start_resolution(user_id=request.user.pk, request=request)
        return Response({"run_id": str(run.id), **job_links(request, job)}, status=status.HTTP_202_ACCEPTED)


def _run_payload(r: ResolutionRun) -> dict:
    return {"id": str(r.id), "status": r.status, "job_id": str(r.job_id) if r.job_id else None,
            "requested_by": str(r.requested_by) if r.requested_by else None, "stats": r.stats, "error": r.error,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "created_at": r.created_at.isoformat()}


class ResolutionRunListView(_Paged):
    required_permissions = {"get": "customers.view"}

    def get(self, request, **kwargs):
        return self.page(request, ResolutionRun.objects.all(), _run_payload)


class ResolutionRunDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "customers.view"}

    def get(self, request, pk, **kwargs):
        return Response(_run_payload(_get(ResolutionRun, pk)))


class MatchListView(_Paged):
    required_permissions = {"get": "customers.review"}
    ordering = ("-score", "-created_at")

    def get(self, request, **kwargs):
        qs = IdentityMatch.objects.all()
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status__in=st.split(","))
        paginator = DefaultCursorPagination()
        paginator.ordering = self.ordering
        items = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(services.matches_payload(list(items)))


class MatchDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "customers.review"}

    def get(self, request, pk, **kwargs):
        return Response(services.matches_payload([_get(IdentityMatch, pk)])[0])


class MatchDecisionView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"post": "customers.review"}

    def post(self, request, pk, **kwargs):
        match = _get(IdentityMatch, pk)
        decision = str((request.data or {}).get("decision") or "")
        match = services.decide_match(match, decision, user_id=request.user.pk, request=request,
                                      note=str((request.data or {}).get("note") or ""))
        return Response(services.matches_payload([match])[0])


class SegmentListView(_Paged):
    required_permissions = {"get": "customers.manage", "post": "customers.manage"}

    def get(self, request, **kwargs):
        qs = CustomerSegment.objects.all()
        method = request.query_params.get("method")
        if method:
            qs = qs.filter(method=method)
        return self.page(request, qs, services.segment_payload)

    def post(self, request, **kwargs):
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": ["A JSON object is required."]})
        segments = services.create_segment(request.data, user_id=request.user.pk, request=request)
        if request.data.get("method") == "rfm":
            return Response({"items": [services.segment_payload(s) for s in segments]}, status=status.HTTP_201_CREATED)
        return Response(services.segment_payload(segments[0]), status=status.HTTP_201_CREATED)


class SegmentDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "customers.manage", "delete": "customers.manage"}

    def get(self, request, pk, **kwargs):
        return Response(services.segment_payload(_get(CustomerSegment, pk)))

    def delete(self, request, pk, **kwargs):
        from apps.core import audit

        segment = _get(CustomerSegment, pk)
        audit.record("customers.segment.delete", target=segment, request=request, metadata={"name": segment.name})
        segment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SegmentMembersView(_Paged):
    required_permissions = {"get": "customers.manage"}

    def get(self, request, pk, **kwargs):
        segment = _get(CustomerSegment, pk)
        qs = CustomerSegmentMember.objects.filter(segment_id=segment.id).select_related("profile")
        return self.page(request, qs, lambda m: {**services.profile_payload(m.profile), "segment": m.attributes})


class SegmentRefreshView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"post": "customers.manage"}

    def post(self, request, pk, **kwargs):
        from apps.core import audit

        segment = services.refresh_segment(_get(CustomerSegment, pk))
        audit.record("customers.segment.refresh", target=segment, request=request, metadata={"size": segment.size})
        return Response(services.segment_payload(segment))


class ScoringRunView(_Paged):
    required_permissions = {"get": "customers.view", "post": "customers.manage"}

    def get(self, request, **kwargs):
        from .models import CustomerScoringRun
        from .scoring import run_payload

        qs = CustomerScoringRun.objects.all()
        score_type = request.query_params.get("score_type")
        if score_type:
            qs = qs.filter(score_type=score_type)
        return self.page(request, qs, run_payload)

    def post(self, request, **kwargs):
        run, job = services.start_scoring(request.data if isinstance(request.data, dict) else {},
                                          user_id=request.user.pk, request=request)
        return Response({"run_id": str(run.id), "score_type": run.score_type,
                         "model_id": str(run.model_id) if run.model_id else None,
                         "model_version": run.model_version, "selection": run.selection, **job_links(request, job)},
                        status=status.HTTP_202_ACCEPTED)


class FeaturesPublishView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"post": "customers.manage"}

    def post(self, request, **kwargs):
        data = request.data if isinstance(request.data, dict) else {}
        version = services.publish_features(user_id=request.user.pk, kind=str(data.get("kind") or "current"),
                                            request=request)
        body = {"dataset_id": str(version.dataset_id), "version_id": str(version.id), "version": version.version,
                "status": version.status}
        if version.job_id:
            base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{version.job_id}"
            body.update({"job_id": str(version.job_id), "status_url": base, "events_url": f"{base}/events"})
        return Response(body, status=status.HTTP_202_ACCEPTED)
