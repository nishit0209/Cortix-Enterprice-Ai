from __future__ import annotations

from rest_framework import serializers

from .models import CLASSIFICATIONS, Dataset, DatasetColumn, DatasetVersion, Upload

STATS_KEYS = ("null_pct", "null_count", "distinct", "min", "max", "mean", "stddev", "p5", "p50", "p95",
              "top_values", "histogram", "invalid_count", "min_length", "max_length", "avg_length",
              "true_count", "false_count")


class UploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Upload
        fields = ["id", "filename", "size_bytes", "status", "file_kind", "content_type", "sha256", "reject_reason",
                  "created_at"]
        read_only_fields = fields


class VersionSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = DatasetVersion
        fields = ["id", "version", "row_count", "status", "wh_table", "created_at"]


class VersionSerializer(serializers.ModelSerializer):
    upload_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = DatasetVersion
        fields = ["id", "version", "row_count", "byte_size", "status", "wh_table", "schema_hash", "upload_id",
                  "job_id", "error", "created_at", "ready_at"]


class DatasetSerializer(serializers.ModelSerializer):
    current_version = VersionSummarySerializer(read_only=True)
    quality_score = serializers.SerializerMethodField()

    class Meta:
        model = Dataset
        fields = [
            "id", "name", "slug", "description", "classification", "status", "row_count", "column_count",
            "quality_score", "current_version", "created_at", "updated_at", "tags", "source_kind",
            "classification_attested_at", "owner_id", "last_error", "last_job_id",
        ]

    def get_quality_score(self, obj) -> float | None:
        return float(obj.quality_score) if obj.quality_score is not None else None


class DatasetCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    upload_id = serializers.UUIDField()
    classification = serializers.ChoiceField(choices=[c for c, _ in CLASSIFICATIONS], required=False)
    attest = serializers.BooleanField(required=False, default=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    tags = serializers.ListField(child=serializers.CharField(max_length=50), required=False, max_length=20)
    options = serializers.DictField(required=False)

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("The name cannot be blank.")
        return value.strip()

    def validate_options(self, value: dict) -> dict:
        allowed = {"sheet", "delimiter", "has_header", "encoding"}
        unknown = set(value) - allowed
        if unknown:
            raise serializers.ValidationError(f"Unknown options: {', '.join(sorted(unknown))}.")
        if "delimiter" in value and (not isinstance(value["delimiter"], str) or len(value["delimiter"]) != 1):
            raise serializers.ValidationError("delimiter must be a single character.")
        if "has_header" in value and not isinstance(value["has_header"], bool):
            raise serializers.ValidationError("has_header must be a boolean.")
        for key in ("sheet", "encoding"):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > 100):
                raise serializers.ValidationError(f"{key} must be a short string.")
        return value


class DatasetUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    tags = serializers.ListField(child=serializers.CharField(max_length=50), required=False, max_length=20)

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("The name cannot be blank.")
        return value.strip()


class ClassificationSerializer(serializers.Serializer):
    classification = serializers.ChoiceField(choices=[c for c, _ in CLASSIFICATIONS])
    attest = serializers.BooleanField(required=False, default=False)


class NewVersionSerializer(serializers.Serializer):
    upload_id = serializers.UUIDField()
    options = serializers.DictField(required=False)

    validate_options = DatasetCreateSerializer.validate_options


class ColumnSerializer(serializers.ModelSerializer):
    stats = serializers.SerializerMethodField()

    class Meta:
        model = DatasetColumn
        fields = ["name", "source_name", "ordinal", "data_type", "semantic_type", "nullable", "is_pii", "pii_type",
                  "stats"]

    def get_stats(self, obj) -> dict:
        stats = obj.stats or {}
        return {k: stats.get(k) for k in STATS_KEYS if k in stats}
