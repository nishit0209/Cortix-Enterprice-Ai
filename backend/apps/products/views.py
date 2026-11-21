"""Product 360 API (API_SPECIFICATION Phase 7). All routes under /api/v1/orgs/{org}/products.

GET    /products?search=&brand=&category=      products.view     masters (cursor envelope)
GET    /products/{id}                          products.view     master + records, matches, quality, corrections
GET    /products/{id}/attributes               products.view
GET    /products/settings                      products.view     thresholds / weights / brand aliases (+ defaults)
PATCH  /products/settings                      products.manage
GET    /products/sources                       products.view
POST   /products/sources                       products.manage   {dataset_id, mapping, name?, source_system?, priority?}
GET    /products/sources/{id}                  products.view
DELETE /products/sources/{id}                  products.manage
POST   /products/normalize                     products.manage   202 {run_id, job_id, status_url, events_url}
POST   /products/enrich                        products.manage   202 (local rules -> proposed corrections)
GET    /products/runs[/{id}]                   products.view
GET    /products/matches?status=pending_review products.review
GET    /products/matches/{id}                  products.review
POST   /products/matches/{id}/decision         products.review   {decision: confirm|reject, note?}
GET    /products/corrections?status=proposed   products.review
POST   /products/corrections                   products.manage   {master_id, field, value, reason?}
POST   /products/corrections/{id}/decision     products.review   {decision: approve|reject, note?} applied on approve
POST   /products/search                        products.view     {query, k?, brand?, category?} -> search_mode
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
from .models import ProductCorrection, ProductMaster, ProductMatch, ProductRun, ProductSource


def job_links(request, job) -> dict:
    base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{job.id}"
    return {"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"}


def _get(model, pk):
    obj = model.objects.filter(id=pk).first()
    if obj is None:
        raise NotFound()
    return obj


def _body(request) -> dict:
    if not isinstance(request.data, dict):
        raise ValidationError({"detail": ["A JSON object is required."]})
    return request.data


class _View(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]


class _Paged(_View):
    ordering = "-created_at"

    def page(self, request, qs, render, *, many=False):
        paginator = DefaultCursorPagination()
        paginator.ordering = self.ordering
        items = paginator.paginate_queryset(qs, request, view=self)
        data = render(list(items)) if many else [render(o) for o in items]
        return paginator.get_paginated_response(data)


class MasterListView(_Paged):
    required_permissions = {"get": "products.view"}

    def get(self, request, **kwargs):
        qs = ProductMaster.objects.all()
        term = (request.query_params.get("search") or "").strip()
        if term:
            qs = qs.filter(Q(name__icontains=term) | Q(sku_normalized__icontains=term) | Q(brand__icontains=term))
        for field in ("brand", "category"):
            value = request.query_params.get(field)
            if value:
                qs = qs.filter(**{f"{field}__iexact": value})
        return self.page(request, qs, services.master_payload)


class MasterDetailView(_View):
    required_permissions = {"get": "products.view"}

    def get(self, request, pk, **kwargs):
        return Response(services.master_detail(_get(ProductMaster, pk)))


class MasterAttributesView(_View):
    required_permissions = {"get": "products.view"}

    def get(self, request, pk, **kwargs):
        master = _get(ProductMaster, pk)
        return Response({"items": [services.attribute_payload(a) for a in master.attribute_rows.order_by("key")]})


class SettingsView(_View):
    required_permissions = {"get": "products.view", "patch": "products.manage"}

    def get(self, request, **kwargs):
        return Response(services.settings_payload(services.get_settings()))

    def patch(self, request, **kwargs):
        return Response(services.settings_payload(services.update_settings(_body(request), request=request)))


class SourceListView(_Paged):
    required_permissions = {"get": "products.view", "post": "products.manage"}

    def get(self, request, **kwargs):
        return self.page(request, ProductSource.objects.all(), services.source_payload)

    def post(self, request, **kwargs):
        source = services.create_source(_body(request), user_id=request.user.pk, request=request)
        return Response(services.source_payload(source), status=status.HTTP_201_CREATED)


class SourceDetailView(_View):
    required_permissions = {"get": "products.view", "delete": "products.manage"}

    def get(self, request, pk, **kwargs):
        return Response(services.source_payload(_get(ProductSource, pk)))

    def delete(self, request, pk, **kwargs):
        services.delete_source(_get(ProductSource, pk), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class _RunStartView(_View):
    required_permissions = {"post": "products.manage"}
    kind = ""

    def post(self, request, **kwargs):
        run, job = services.start_run(self.kind, user_id=request.user.pk, request=request)
        return Response({"run_id": str(run.id), **job_links(request, job)}, status=status.HTTP_202_ACCEPTED)


class NormalizeView(_RunStartView):
    kind = "normalize"


class EnrichView(_RunStartView):
    kind = "enrich"


class RunListView(_Paged):
    required_permissions = {"get": "products.view"}

    def get(self, request, **kwargs):
        qs = ProductRun.objects.all()
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        return self.page(request, qs, services.run_payload)


class RunDetailView(_View):
    required_permissions = {"get": "products.view"}

    def get(self, request, pk, **kwargs):
        return Response(services.run_payload(_get(ProductRun, pk)))


class MatchListView(_Paged):
    required_permissions = {"get": "products.review"}
    ordering = ("-score", "-created_at")

    def get(self, request, **kwargs):
        qs = ProductMatch.objects.all()
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status__in=st.split(","))
        return self.page(request, qs, services.matches_payload, many=True)


class MatchDetailView(_View):
    required_permissions = {"get": "products.review"}

    def get(self, request, pk, **kwargs):
        return Response(services.matches_payload([_get(ProductMatch, pk)])[0])


class MatchDecisionView(_View):
    required_permissions = {"post": "products.review"}

    def post(self, request, pk, **kwargs):
        match = _get(ProductMatch, pk)
        data = request.data if isinstance(request.data, dict) else {}
        match = services.decide_match(match, str(data.get("decision") or ""), user_id=request.user.pk,
                                      request=request, note=str(data.get("note") or ""))
        return Response(services.matches_payload([match])[0])


class CorrectionListView(_Paged):
    required_permissions = {"get": "products.review", "post": "products.manage"}

    def get(self, request, **kwargs):
        qs = ProductCorrection.objects.all()
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status__in=st.split(","))
        master = request.query_params.get("master_id")
        if master:
            qs = qs.filter(master_id=master)
        return self.page(request, qs, services.correction_payload)

    def post(self, request, **kwargs):
        data = _body(request)
        try:
            master = _get(ProductMaster, data.get("master_id"))
        except (ValueError, ValidationError) as exc:
            raise ValidationError({"master_id": ["A product id is required."]}) from exc
        c = services.propose_correction(master, str(data.get("field") or ""), data.get("value"),
                                        user_id=request.user.pk, reason=str(data.get("reason") or ""),
                                        origin="user", request=request)
        return Response(services.correction_payload(c), status=status.HTTP_201_CREATED)


class CorrectionDecisionView(_View):
    required_permissions = {"post": "products.review"}

    def post(self, request, pk, **kwargs):
        c = _get(ProductCorrection, pk)
        data = request.data if isinstance(request.data, dict) else {}
        c = services.decide_correction(c, str(data.get("decision") or ""), user_id=request.user.pk,
                                       request=request, note=str(data.get("note") or ""))
        return Response(services.correction_payload(c))


class SearchView(_View):
    required_permissions = {"post": "products.view"}

    def post(self, request, **kwargs):
        data = _body(request)
        return Response(services.search(data.get("query"), k=data.get("k", 20),
                                        filters={"brand": data.get("brand"), "category": data.get("category")}))
