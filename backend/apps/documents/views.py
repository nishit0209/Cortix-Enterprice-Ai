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
    POST   documents/{id}/extract          {schema_key, mode?=local|ai} -> 201 extraction (needs review)
    GET    documents/{id}/extractions
    POST   documents/compare               {a, b, summary?} -> local section diff (+ policy-gated summary)
    GET/POST            document-collections            GET/PATCH/DELETE document-collections/{id}
    GET    document-extractions/{id}       POST document-extractions/{id}/review {decision, corrections?, note?}
    GET    document-tables/{id}            POST document-tables/{id}/promote {name?, classification?, attest?}
"""

from __future__ import annotations

from django.db.models import Count
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.pagination import DefaultCursorPagination
from apps.core.permissions import HasOrgPermission, tenant_has_perm
from apps.core.tenancy.views import TenantGenericViewSet

from . import filetypes, services
from .exceptions import DocumentNotReady, DocumentRejected, DocumentTooLarge
from .models import (
    Document,
    DocumentChunk,
    DocumentCollection,
    DocumentEntity,
    DocumentPage,
    DocumentTable,
    ExtractionResult,
)
from .serializers import (
    AskSerializer,
    ChunkSerializer,
    ClassificationSerializer,
    CollectionSerializer,
    CollectionWriteSerializer,
    CompareSerializer,
    DocumentDetailSerializer,
    DocumentSerializer,
    DocumentUpdateSerializer,
    DocumentUploadSerializer,
    EntitySerializer,
    ExtractionSerializer,
    ExtractSerializer,
    PageSerializer,
    PromoteTableSerializer,
    ReviewSerializer,
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
        "summary": "documents.view", "search": "documents.view", "extractions": "documents.view",
        "compare": "documents.view", "extract": "documents.manage",
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
            for field in ("status", "doc_type", "classification", "collection_id"):
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
            attest=data.get("attest", False), user_id=request.user.pk, request=request,
            collection_id=data.get("collection_id"))
        return Response({"document": DocumentSerializer(doc).data, **job_links(request, job)},
                        status=status.HTTP_202_ACCEPTED)

    def partial_update(self, request, *args, **kwargs):
        doc = self.get_object()  # another org's id answers 404 before the body is validated
        ser = DocumentUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        doc = services.update_document(doc, title=data.get("title"), request=request)
        if "collection_id" in data:
            try:
                doc = services.set_document_collection(doc, data["collection_id"], request=request)
            except NotFound as exc:
                raise ValidationError({"collection_id": ["Unknown collection."]}) from exc
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

    # ------------------------------------------------------------------------------------- W5 actions
    @action(detail=True, methods=["post"])
    def extract(self, request, *args, **kwargs):
        doc = self.get_object()  # another org's id answers 404 before the body is validated
        ser = ExtractSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        mode = ser.validated_data["mode"]
        if mode == "ai" and not tenant_has_perm(request, "documents.ask"):
            raise PermissionDenied("AI extraction needs the documents.ask permission.")
        result = services.extract(doc, ser.validated_data["schema_key"], mode=mode, user_id=request.user.pk,
                                  request=request)
        return Response(ExtractionSerializer(result).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def extractions(self, request, *args, **kwargs):
        doc = self.get_object()
        qs = ExtractionResult.objects.filter(document=doc).order_by("-created_at")[:100]
        return Response({"items": ExtractionSerializer(qs, many=True).data})

    @action(detail=False, methods=["post"])
    def compare(self, request, *args, **kwargs):
        ser = CompareSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if d["a"] == d["b"]:
            raise ValidationError({"b": ["Choose two different documents."]})
        if d["summary"] and not tenant_has_perm(request, "documents.ask"):
            raise PermissionDenied("An AI summary of the differences needs the documents.ask permission.")
        a, b = services.get_document(d["a"]), services.get_document(d["b"])
        return Response(services.compare(a, b, summary=d["summary"], user_id=request.user.pk))


class DocumentCollectionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"list": "documents.view", "retrieve": "documents.view", "*": "documents.manage"}
    serializer_class = CollectionSerializer

    def get_queryset(self):
        return DocumentCollection.objects.annotate(n_documents=Count("documents")).order_by("name")

    def get_object(self):
        coll = services.get_collection(self.kwargs.get("pk"))
        return self.get_queryset().get(id=coll.id)

    def create(self, request, *args, **kwargs):
        ser = CollectionWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if not d.get("name"):
            raise ValidationError({"name": ["A name is required."]})
        coll = services.create_collection(name=d["name"], description=d.get("description", ""),
                                          classification=d.get("classification", "internal"),
                                          user_id=request.user.pk, request=request)
        return Response(CollectionSerializer(coll).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        coll = self.get_object()
        ser = CollectionWriteSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        services.update_collection(coll, ser.validated_data, request=request)
        return Response(CollectionSerializer(self.get_object()).data)

    def destroy(self, request, *args, **kwargs):
        services.delete_collection(self.get_object(), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ExtractionViewSet(mixins.RetrieveModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"retrieve": "documents.view", "*": "documents.manage"}
    serializer_class = ExtractionSerializer

    def get_queryset(self):
        return ExtractionResult.objects.all()

    def get_object(self):
        return services.get_extraction(self.kwargs.get("pk"))

    @action(detail=True, methods=["post"])
    def review(self, request, *args, **kwargs):
        result = self.get_object()
        ser = ReviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        result = services.review_extraction(result, decision=d["decision"], corrections=d.get("corrections"),
                                            note=d.get("note", ""), user_id=request.user.pk, request=request)
        return Response(ExtractionSerializer(result).data)


class DocumentTableViewSet(mixins.RetrieveModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"retrieve": "documents.view", "*": "datasets.create"}
    serializer_class = TableSerializer

    def get_queryset(self):
        return DocumentTable.objects.all()

    def get_object(self):
        return services.get_table(self.kwargs.get("pk"))

    @action(detail=True, methods=["post"])
    def promote(self, request, *args, **kwargs):
        table = self.get_object()
        if not tenant_has_perm(request, "documents.view"):
            raise PermissionDenied()
        ser = PromoteTableSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        out = services.promote_table(table, user_id=request.user.pk, name=d.get("name", ""),
                                     classification=d.get("classification"), attest=d.get("attest", False),
                                     request=request)
        code = status.HTTP_201_CREATED if out["status"] == "ready" else status.HTTP_202_ACCEPTED
        return Response(out, status=code)
