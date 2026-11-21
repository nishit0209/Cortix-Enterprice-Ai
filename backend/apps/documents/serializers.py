from __future__ import annotations

from rest_framework import serializers

from .models import (
    CLASSIFICATIONS,
    Document,
    DocumentChunk,
    DocumentCollection,
    DocumentEntity,
    DocumentPage,
    DocumentTable,
    ExtractionResult,
)
from .structured import schema_keys


class DocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = [
            "id", "title", "filename", "mime", "file_kind", "size_bytes", "sha256", "classification",
            "classification_attested_at", "doc_type", "status", "page_count", "language", "chunk_count",
            "ocr_pages", "ocr_unavailable_pages", "error", "error_code", "warnings", "uploaded_by", "last_job_id",
            "created_at", "updated_at", "processed_at", "embedding_status", "collection_id",
        ]
        read_only_fields = fields


DOWNLOAD_URL_TTL_S = 600


class DocumentDetailSerializer(DocumentSerializer):
    """Detail payload. `download_url` is a short-lived signed link to the original upload (`apps.core.storage.
    presigned_get`: `/api/v1/files/<token>` on the fs backend, an S3 presigned URL otherwise). Only the detail
    route returns it, after its `documents.view` check; `null` when the stored key is unusable."""

    download_url = serializers.SerializerMethodField()
    download_url_expires_in = serializers.SerializerMethodField()

    class Meta(DocumentSerializer.Meta):
        fields = [*DocumentSerializer.Meta.fields, "languages", "doc_type_scores", "summary", "download_url",
                  "download_url_expires_in"]
        read_only_fields = fields

    def get_download_url(self, obj) -> str | None:
        from .services import document_download_url

        return document_download_url(obj, expires_s=DOWNLOAD_URL_TTL_S)

    def get_download_url_expires_in(self, obj) -> int:
        return DOWNLOAD_URL_TTL_S


class DocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    collection_id = serializers.UUIDField(required=False, allow_null=True)
    title = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    classification = serializers.ChoiceField(choices=[c for c, _ in CLASSIFICATIONS], required=False)
    attest = serializers.BooleanField(required=False, default=False)


class DocumentUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255, required=False)
    collection_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Give title and/or collection_id.")
        return attrs


class ClassificationSerializer(serializers.Serializer):
    classification = serializers.ChoiceField(choices=[c for c, _ in CLASSIFICATIONS])
    attest = serializers.BooleanField(required=False, default=False)


class PageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentPage
        fields = ["id", "number", "text", "method", "ocr_confidence", "char_count", "language", "label"]
        read_only_fields = fields


class ChunkSerializer(serializers.ModelSerializer):
    has_embedding = serializers.SerializerMethodField()

    class Meta:
        model = DocumentChunk
        fields = ["id", "ordinal", "text", "page", "page_to", "section", "token_count", "has_embedding",
                  "embedding_pending"]
        read_only_fields = fields

    def get_has_embedding(self, obj) -> bool:
        return obj.embedding is not None


class EntitySerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentEntity
        fields = ["id", "kind", "value", "normalized", "is_pii", "confidence", "page", "chunk_id", "start", "end"]
        read_only_fields = fields


class TableSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentTable
        fields = ["id", "index", "page", "title", "header", "rows", "n_rows", "n_cols", "truncated",
                  "promoted_dataset_id"]
        read_only_fields = fields


class AskSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=1000)
    document_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=50)


class CollectionSerializer(serializers.ModelSerializer):
    document_count = serializers.SerializerMethodField()

    class Meta:
        model = DocumentCollection
        fields = ["id", "name", "description", "classification", "created_by", "created_at", "updated_at",
                  "document_count"]
        read_only_fields = ["id", "created_by", "created_at", "updated_at", "document_count"]

    def get_document_count(self, obj) -> int:
        value = getattr(obj, "n_documents", None)
        return value if value is not None else obj.documents.count()


class CollectionWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    classification = serializers.ChoiceField(choices=[c for c, _ in CLASSIFICATIONS], required=False)


class ExtractSerializer(serializers.Serializer):
    schema_key = serializers.ChoiceField(choices=schema_keys())
    mode = serializers.ChoiceField(choices=["local", "ai"], required=False, default="local")


class ExtractionSerializer(serializers.ModelSerializer):
    fields_found = serializers.SerializerMethodField()
    fields_total = serializers.SerializerMethodField()

    class Meta:
        model = ExtractionResult
        fields = ["id", "document_id", "schema_key", "method", "status", "result", "field_citations",
                  "field_confidence", "corrections", "reason", "fields_found", "fields_total", "approval_request_id",
                  "created_by", "reviewed_by", "reviewed_at", "review_note", "created_at", "updated_at"]
        read_only_fields = fields

    def get_fields_found(self, obj) -> int:
        return sum(1 for v in (obj.result or {}).values() if v not in (None, "", []))

    def get_fields_total(self, obj) -> int:
        return len(obj.result or {})


class ReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "reject"])
    corrections = serializers.DictField(required=False, default=dict)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)


class CompareSerializer(serializers.Serializer):
    a = serializers.UUIDField()
    b = serializers.UUIDField()
    summary = serializers.BooleanField(required=False, default=False)


class PromoteTableSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    classification = serializers.ChoiceField(choices=[c for c, _ in CLASSIFICATIONS], required=False)
    attest = serializers.BooleanField(required=False, default=False)
