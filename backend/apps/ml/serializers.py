"""ML workspace payloads (API_SPECIFICATION Phase 9). Read serializers expose aggregates only, never data rows."""

from __future__ import annotations

from rest_framework import serializers

from .engine.algorithms import ALGORITHM_LABELS
from .models import TASK_TYPES, DatasetSnapshot, Experiment, MLModel, MLProject, ModelVersion

TASKS = [t for t, _ in TASK_TYPES]


class ProjectSerializer(serializers.ModelSerializer):
    target_column = serializers.CharField(source="target", read_only=True)
    leakage_dropped = serializers.JSONField(source="leakage", read_only=True)
    has_eda = serializers.SerializerMethodField()

    class Meta:
        model = MLProject
        fields = [
            "id", "name", "description", "business_goal", "dataset_id", "target", "target_column", "task_type",
            "task_type_source", "time_column", "features", "column_kinds", "excluded_by_policy", "leakage",
            "leakage_dropped", "primary_metric", "eda_status", "eda_job_id", "has_eda", "status", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_has_eda(self, obj) -> bool:
        return bool(obj.eda)


class ProjectDetailSerializer(ProjectSerializer):
    class Meta(ProjectSerializer.Meta):
        fields = [*ProjectSerializer.Meta.fields, "profile"]
        read_only_fields = fields


class ProjectCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    business_goal = serializers.CharField(required=False, allow_blank=True, default="")
    dataset_id = serializers.UUIDField()
    target = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    target_column = serializers.CharField(required=False, allow_blank=True, max_length=255, write_only=True)
    task_type = serializers.ChoiceField(choices=TASKS, required=False, allow_blank=True)
    time_column = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    features = serializers.ListField(child=serializers.CharField(max_length=255), required=False, max_length=500)
    primary_metric = serializers.CharField(required=False, allow_blank=True, max_length=40)

    def validate(self, attrs):
        alias = attrs.pop("target_column", None)
        if alias and not attrs.get("target"):
            attrs["target"] = alias
        return attrs


class ProjectUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    business_goal = serializers.CharField(required=False, allow_blank=True)
    primary_metric = serializers.CharField(required=False, allow_blank=True, max_length=40)
    status = serializers.ChoiceField(choices=["active", "archived"], required=False)
    features = serializers.ListField(child=serializers.CharField(max_length=255), required=False, max_length=500)


class EdaRequestSerializer(serializers.Serializer):
    narrate = serializers.BooleanField(required=False, default=False)


class SnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = DatasetSnapshot
        fields = [
            "id", "project_id", "dataset_id", "dataset_version_id", "dataset_version", "columns", "excluded_by_policy",
            "row_count", "source_row_count", "sha256", "sample_info", "split_spec", "status", "job_id", "error",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class SnapshotRequestSerializer(serializers.Serializer):
    test_size = serializers.FloatField(required=False, min_value=0.05, max_value=0.5)
    random_state = serializers.IntegerField(required=False, min_value=0, max_value=2**31 - 1)


class ExperimentSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    class Meta:
        model = Experiment
        fields = [
            "id", "project_id", "snapshot_id", "name", "algorithm", "label", "task_type", "hyperparameters",
            "random_state", "tune", "status", "metrics", "primary_metric", "primary_value", "train_rows", "test_rows",
            "duration_s", "recommended", "recommendation_rank", "override_reason", "job_id", "error", "error_code",
            "started_at", "finished_at", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_label(self, obj) -> str:
        return ALGORITHM_LABELS.get(obj.algorithm, obj.algorithm)


class ExperimentDetailSerializer(ExperimentSerializer):
    class Meta(ExperimentSerializer.Meta):
        fields = [
            *ExperimentSerializer.Meta.fields, "effective_params", "confusion_matrix", "residuals", "curves",
            "feature_importances", "shap_summary", "feature_schema", "warnings", "sample_info", "artifact_sha256",
            "artifact_size", "lib_versions",
        ]
        read_only_fields = fields


class ExperimentRequestSerializer(serializers.Serializer):
    algorithm = serializers.CharField(required=False, max_length=40)
    algorithms = serializers.ListField(child=serializers.CharField(max_length=40), required=False, max_length=6)
    hyperparameters = serializers.DictField(required=False, default=dict)
    random_state = serializers.IntegerField(required=False, min_value=0, max_value=2**31 - 1)
    snapshot_id = serializers.UUIDField(required=False)
    name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    tune = serializers.BooleanField(required=False, default=False)
    override_reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class RegisterSerializer(serializers.Serializer):
    model_id = serializers.UUIDField(required=False)
    model_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    name = serializers.CharField(required=False, allow_blank=True, max_length=200, write_only=True)
    description = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        alias = attrs.pop("name", None)
        if alias and not attrs.get("model_name"):
            attrs["model_name"] = alias
        return attrs


class VersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelVersion
        fields = [
            "id", "model_id", "version", "experiment_id", "algorithm", "artifact_sha256", "artifact_size",
            "feature_schema", "metrics", "lib_versions", "dataset_id", "dataset_version_id", "snapshot_id", "stage",
            "promoted_by", "promoted_at", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class ModelSerializer(serializers.ModelSerializer):
    latest_version = serializers.SerializerMethodField()
    production_version = serializers.SerializerMethodField()

    class Meta:
        model = MLModel
        fields = [
            "id", "project_id", "name", "description", "task_type", "target", "latest_version", "production_version",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def _versions(self, obj) -> list:
        return list(obj.versions.all())

    def get_latest_version(self, obj) -> int | None:
        versions = self._versions(obj)
        return max((v.version for v in versions), default=None)

    def get_production_version(self, obj) -> int | None:
        return next((v.version for v in self._versions(obj) if v.stage == "production"), None)


class ModelDetailSerializer(ModelSerializer):
    versions = serializers.SerializerMethodField()

    class Meta(ModelSerializer.Meta):
        fields = [*ModelSerializer.Meta.fields, "label_dataset_id", "versions"]
        read_only_fields = fields

    def get_versions(self, obj) -> list[dict]:
        return VersionSerializer(sorted(self._versions(obj), key=lambda v: -v.version), many=True).data


class PromoteSerializer(serializers.Serializer):
    stage = serializers.ChoiceField(choices=["staging", "production", "archived"])


class PredictSerializer(serializers.Serializer):
    rows = serializers.JSONField(required=False)
    version = serializers.IntegerField(required=False, min_value=1, allow_null=True)
    explain = serializers.BooleanField(required=False, default=False)
    horizon = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs.get("rows") is None and attrs.get("horizon") is None:
            raise serializers.ValidationError({"rows": ["Send `rows` (or `horizon` for a forecast model)."]})
        return attrs


class BatchPredictionSerializer(serializers.Serializer):
    dataset_id = serializers.UUIDField()
    version = serializers.IntegerField(required=False, min_value=1, allow_null=True)
    output_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    output_dataset_id = serializers.UUIDField(required=False, allow_null=True)
    explain = serializers.BooleanField(required=False, default=False)
    attest = serializers.BooleanField(required=False, default=False)
