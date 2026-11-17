from rest_framework import serializers

from .models import AuditEvent, Job


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = ["id", "type", "status", "progress", "message", "result", "error", "created_at", "finished_at"]


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = [
            "id", "occurred_at", "actor_type", "actor_id", "action", "target_type", "target_id",
            "request_id", "ip", "changes", "metadata",
        ]
