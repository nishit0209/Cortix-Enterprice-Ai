from __future__ import annotations

from rest_framework import serializers

from .models import CLASSIFICATIONS, Document, DocumentChunk, DocumentEntity, DocumentPage, DocumentTable


class DocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = [
            "id", "title", "filename", "mime", "file_kind", "size_bytes", "sha256", "classification",
            "classification_attested_at", "doc_type", "status", "page_count", "language", "chunk_count",
            "ocr_pages", "ocr_unavailable_pages", "error", "error_code", "warnings", "uploaded_by", "last_job_id",
            "created_at", "updated_at", "processed_at", "embedding_status",
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
    title = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    classification = serializers.ChoiceField(choices=[c for c, _ in CLASSIFICATIONS], required=False)
    attest = serializers.BooleanField(required=False, default=False)


class DocumentUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)


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
        fields = ["id", "index", "page", "title", "header", "rows", "n_rows", "n_cols", "truncated"]
        read_only_fields = fields


class AskSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=1000)
    document_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=50)
