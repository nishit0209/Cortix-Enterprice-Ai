"""Document intelligence API (mounted under /api/v1/orgs/{org}/).

    GET    documents                       list (cursor-paginated; ?status= ?doc_type= ?classification= ?q=)
    POST   documents                       multipart upload (file, title?, classification?, attest?) -> 202 + job
    GET    documents/search?q=&k=&document_ids=a,b   hybrid search (pgvector + full-text, RRF)
    POST   documents/ask                   {question, document_ids?} -> generative (policy-gated) or extractive
    GET    documents/{id}                  detail
    PATCH  documents/{id}                  {title}
    DELETE documents/{id}
    GET    documents/{id}/pages | chunks   cursor-paginated
    GET    documents/{id}/entities | tables
    GET    documents/{id}/summary?mode=extractive|generative
    POST   documents/{id}/classification   {classification, attest}
    POST   documents/{id}/reprocess        -> 202 + job
"""

from __future__ import annotations

from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.pagination import DefaultCursorPagination
from apps.core.permissions import HasOrgPermission, tenant_has_perm
from apps.core.tenancy.views import TenantGenericViewSet

from . import filetypes, services
from .exceptions import DocumentNotReady, DocumentRejected, DocumentTooLarge
from .models import Document, DocumentChunk, DocumentEntity, DocumentPage, DocumentTable
from .serializers import (
    AskSerializer,
    ChunkSerializer,
    ClassificationSerializer,
    DocumentDetailSerializer,
    DocumentSerializer,
    DocumentUpdateSerializer,
    DocumentUploadSerializer,
    EntitySerializer,
    PageSerializer,
    TableSerializer,
)

MULTIPART_OVERHEAD = 64 * 1024
MAX_ENTITIES_RESPONSE = 2000


class _PagePagination(DefaultCursorPagination):
    ordering = ("number",)
    page_size = 20


class _ChunkPagination(DefaultCursorPagination):
    ordering = ("ordinal",)
    page_size = 20


def job_links(request, job) -> dict:
    base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{job.id}"
    return {"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"}


class DocumentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {
        "list": "documents.view", "retrieve": "documents.view", "pages": "documents.view",
        "chunks": "documents.view", "entities": "documents.view", "tables": "documents.view",
        "summary": "documents.view", "search": "documents.view",
        "create": "documents.manage", "partial_update": "documents.manage", "destroy": "documents.manage",
        "classification": "documents.manage", "reprocess": "documents.manage",
        "ask": "documents.ask",
        "*": "documents.manage",
    }
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = DocumentSerializer

    def get_queryset(self):
        qs = Document.objects.all()
        if self.action == "list":
            params = self.request.query_params
            for field in ("status", "doc_type", "classification"):
                value = params.get(field)
                if value:
                    qs = qs.filter(**{field: value})
            if params.get("q"):
                qs = qs.filter(title__icontains=params["q"][:200])
        return qs

    def get_serializer_class(self):
        return DocumentDetailSerializer if self.action == "retrieve" else DocumentSerializer

    def get_object(self):
        return services.get_document(self.kwargs.get("pk"))

    # ------------------------------------------------------------------------------------ mutations
    def create(self, request, *args, **kwargs):
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        limit = filetypes.max_upload_bytes()
        if length > limit + MULTIPART_OVERHEAD:
            raise DocumentTooLarge(f"The request is larger than the {limit} byte document limit.")
        if len(request.FILES.getlist("file")) > 1:
            raise DocumentRejected("Upload one file per request.", code="upload_multiple_files")
        ser = DocumentUploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        doc, job = services.create_document(
            data["file"], title=data.get("title", ""), classification=data.get("classification"),
            attest=data.get("attest", False), user_id=request.user.pk, request=request)
        return Response({"document": DocumentSerializer(doc).data, **job_links(request, job)},
                        status=status.HTTP_202_ACCEPTED)

    def partial_update(self, request, *args, **kwargs):
        doc = self.get_object()  # another org's id answers 404 before the body is validated
        ser = DocumentUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        doc = services.update_document(doc, title=ser.validated_data["title"], request=request)
        return Response(DocumentDetailSerializer(doc).data)

    def destroy(self, request, *args, **kwargs):
        services.delete_document(self.get_object(), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def classification(self, request, *args, **kwargs):
        doc = self.get_object()
        ser = ClassificationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        doc = services.set_classification(doc, ser.validated_data["classification"],
                                          attest=ser.validated_data.get("attest", False),
                                          user_id=request.user.pk, request=request)
        return Response(DocumentDetailSerializer(doc).data)

    @action(detail=True, methods=["post"])
    def reprocess(self, request, *args, **kwargs):
        doc = self.get_object()
        if doc.status == "processing":
            raise DocumentNotReady("The document is already being processed.")
        job = services.reprocess(doc, user_id=request.user.pk, request=request)
        return Response({"document": DocumentSerializer(doc).data, **job_links(request, job)},
                        status=status.HTTP_202_ACCEPTED)

    # --------------------------------------------------------------------------------- read actions
    def _paginated(self, qs, serializer_cls, paginator_cls):
        paginator = paginator_cls()
        page = paginator.paginate_queryset(qs, self.request, view=self)
        return paginator.get_paginated_response(serializer_cls(page, many=True).data)

    @action(detail=True, methods=["get"])
    def pages(self, request, *args, **kwargs):
        doc = self.get_object()
        return self._paginated(DocumentPage.objects.filter(document=doc), PageSerializer, _PagePagination)

    @action(detail=True, methods=["get"])
    def chunks(self, request, *args, **kwargs):
        doc = self.get_object()
        return self._paginated(DocumentChunk.objects.filter(document=doc), ChunkSerializer, _ChunkPagination)

    @action(detail=True, methods=["get"])
    def entities(self, request, *args, **kwargs):
        doc = self.get_object()
        qs = DocumentEntity.objects.filter(document=doc).order_by("page", "start")
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        if request.query_params.get("pii") in ("true", "1"):
            qs = qs.filter(is_pii=True)
        items = EntitySerializer(qs[:MAX_ENTITIES_RESPONSE], many=True).data
        return Response({"items": items, "truncated": qs.count() > MAX_ENTITIES_RESPONSE})

    @action(detail=True, methods=["get"])
    def tables(self, request, *args, **kwargs):
        doc = self.get_object()
        return Response({"items": TableSerializer(DocumentTable.objects.filter(document=doc), many=True).data})

    @action(detail=True, methods=["get"])
    def summary(self, request, *args, **kwargs):
        doc = self.get_object()
        mode = request.query_params.get("mode", "extractive")
        if mode == "generative" and not tenant_has_perm(request, "documents.ask"):
            raise PermissionDenied("Generative summaries need the documents.ask permission.")
        return Response(services.summarize(doc, mode=mode, user_id=request.user.pk))

    @action(detail=False, methods=["get"])
    def search(self, request, *args, **kwargs):
        q = (request.query_params.get("q") or "").strip()
        if not q:
            raise ValidationError({"q": ["A search query is required."]})
        try:
            k = int(request.query_params.get("k") or services.DEFAULT_K)
        except ValueError as exc:
            raise ValidationError({"k": ["Must be an integer."]}) from exc
        ids = services.resolve_document_ids(request.query_params.get("document_ids"))
        return Response(services.search_payload(services.hybrid_search(q, document_ids=ids, k=k)))

    @action(detail=False, methods=["post"])
    def ask(self, request, *args, **kwargs):
        ser = AskSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ids = services.resolve_document_ids(ser.validated_data.get("document_ids"))
        return Response(services.ask(ser.validated_data["question"], document_ids=ids, user_id=request.user.pk))
