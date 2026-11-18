from __future__ import annotations

from rest_framework import serializers


class PipelineCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True, default="")
    dag = serializers.JSONField(required=False, allow_null=True)
    max_retries = serializers.IntegerField(min_value=0, max_value=10, required=False)
    retry_backoff_s = serializers.IntegerField(min_value=1, max_value=3600, required=False)
    notify_on_failure = serializers.BooleanField(required=False, default=True)


class PipelineUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, trim_whitespace=True, required=False)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    dag = serializers.JSONField(required=False)
    status = serializers.ChoiceField(choices=["active", "paused"], required=False)
    max_retries = serializers.IntegerField(min_value=0, max_value=10, required=False)
    retry_backoff_s = serializers.IntegerField(min_value=1, max_value=3600, required=False)
    notify_on_failure = serializers.BooleanField(required=False)


class ScheduleSerializer(serializers.Serializer):
    cron = serializers.CharField(max_length=100)
    timezone = serializers.CharField(max_length=64, required=False, default="UTC")
    enabled = serializers.BooleanField(required=False, default=True)


class PreviewSerializer(serializers.Serializer):
    dag = serializers.JSONField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=200, required=False, default=200)


class ValidateSerializer(serializers.Serializer):
    dag = serializers.JSONField()
