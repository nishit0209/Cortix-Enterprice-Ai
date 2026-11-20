from __future__ import annotations

from rest_framework import serializers

from .base import registered_types

NAME_MAX = 200
TABLE_MAX = 63  # PostgreSQL identifier limit (a qualified "schema.table" may be up to 2 * 63 + 1)


class DataSourceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=NAME_MAX, trim_whitespace=True)
    type = serializers.ChoiceField(choices=["postgresql"])
    config = serializers.DictField()
    username = serializers.CharField(max_length=128)
    password = serializers.CharField(max_length=1024, write_only=True, trim_whitespace=False)

    def validate_type(self, value: str) -> str:
        if value not in registered_types():
            raise serializers.ValidationError("Unsupported connector type.")
        return value


class DataSourceUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=NAME_MAX, trim_whitespace=True, required=False)
    config = serializers.DictField(required=False)
    username = serializers.CharField(max_length=128, required=False)
    password = serializers.CharField(max_length=1024, write_only=True, required=False, trim_whitespace=False)
    status = serializers.ChoiceField(choices=["disabled", "pending"], required=False)


class ImportSerializer(serializers.Serializer):
    # a table name, or "schema.table" as the catalog browser lists it (split by the view when `schema` is absent)
    table = serializers.RegexField(r"^[^\x00]{1,127}$", max_length=2 * TABLE_MAX + 1)
    schema = serializers.RegexField(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$", required=False, allow_null=True)
    name = serializers.CharField(max_length=NAME_MAX, trim_whitespace=True)
    columns = serializers.ListField(child=serializers.CharField(max_length=63), required=False, allow_empty=False,
                                    max_length=1000)
    classification = serializers.ChoiceField(choices=["synthetic", "public", "internal", "confidential"],
                                             required=False)
    attest = serializers.BooleanField(required=False, default=False)
    mode = serializers.ChoiceField(choices=["full", "incremental"], required=False, default="full")
    cursor_column = serializers.CharField(max_length=63, required=False, allow_blank=False)
    dataset_id = serializers.UUIDField(required=False)
