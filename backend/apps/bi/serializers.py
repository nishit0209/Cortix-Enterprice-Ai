from __future__ import annotations

from rest_framework import serializers

from .models import Conversation, Message, SavedQuery
from .results import CHART_TYPES
from .services import MAX_QUESTION_CHARS, MAX_SQL_CHARS


class ConversationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Conversation
        fields = ["id", "title", "dataset_id", "created_at", "last_message_at"]
        read_only_fields = fields


class ConversationCreateSerializer(serializers.Serializer):
    dataset_id = serializers.UUIDField()
    title = serializers.CharField(max_length=200, required=False, allow_blank=True, trim_whitespace=True)


class ConversationUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True)


class AskSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=MAX_QUESTION_CHARS, trim_whitespace=True)


class ManualQuerySerializer(serializers.Serializer):
    dataset_id = serializers.UUIDField()
    sql = serializers.CharField(max_length=MAX_SQL_CHARS, trim_whitespace=True)


class FeedbackSerializer(serializers.Serializer):
    value = serializers.ChoiceField(choices=["up", "down"])


class ExportSerializer(serializers.Serializer):
    message_id = serializers.UUIDField()
    format = serializers.ChoiceField(choices=["csv", "xlsx"])


def message_payload(m: Message) -> dict:
    """CONTRACT `Message` (+ additive `query_audit_id`, `feedback`, `explanation`)."""
    has_sql = bool(m.sql_raw or m.sql_executed)
    result = None
    if m.result:
        result = {k: m.result.get(k) for k in ("columns", "rows", "row_count", "truncated")}
    return {
        "id": str(m.id),
        "conversation_id": str(m.conversation_id),
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "error_code": m.error_code or None,
        "created_at": m.created_at,
        "sql": {"raw": m.sql_raw, "executed": m.sql_executed or None, "source": m.sql_source} if has_sql else None,
        "result": result,
        "chart": m.chart,
        "insights": [{k: v for k, v in i.items() if k in ("kind", "text", "data")} for i in (m.insights or [])],
        "assumptions": list(m.assumptions or []),
        "explanation": m.explanation or None,
        "query_audit_id": str(m.query_audit_id) if m.query_audit_id else None,
        "feedback": m.feedback or None,
    }


class ChartSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=sorted(CHART_TYPES))
    x = serializers.CharField(max_length=128, required=False, allow_null=True, allow_blank=True)
    y = serializers.ListField(child=serializers.CharField(max_length=128), required=False, max_length=10)
    series = serializers.CharField(max_length=128, required=False, allow_null=True, allow_blank=True)


class SavedQuerySerializer(serializers.ModelSerializer):
    chart = ChartSerializer(required=False, allow_null=True)
    sql = serializers.CharField(max_length=MAX_SQL_CHARS, trim_whitespace=True)
    question = serializers.CharField(max_length=MAX_QUESTION_CHARS, required=False, allow_blank=True)

    class Meta:
        model = SavedQuery
        fields = ["id", "name", "description", "dataset_id", "question", "sql", "chart", "is_shared", "owner_id",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "owner_id", "created_at", "updated_at"]


class SavedQueryUpdateSerializer(SavedQuerySerializer):
    class Meta(SavedQuerySerializer.Meta):
        read_only_fields = ["id", "owner_id", "dataset_id", "created_at", "updated_at"]


# ---------------------------------------------------------------------------------------------- dashboards
# Shapes are validated here; layout/chart semantics (grid bounds, chart types) in apps.bi.dashboards.
class DashboardCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    is_default = serializers.BooleanField(required=False, default=False)
    is_shared = serializers.BooleanField(required=False, default=False)


class DashboardUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True, required=False)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    is_default = serializers.BooleanField(required=False)
    is_shared = serializers.BooleanField(required=False)
    layout = serializers.ListField(child=serializers.DictField(), required=False, max_length=50)


class WidgetCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True)
    kind = serializers.ChoiceField(choices=["chart", "table", "kpi", "text"])
    saved_query_id = serializers.UUIDField(required=False, allow_null=True)
    message_id = serializers.UUIDField(required=False, allow_null=True)
    chart = serializers.JSONField(required=False, allow_null=True)
    layout = serializers.DictField(required=False)
    text = serializers.CharField(max_length=20_000, required=False, allow_blank=True)


class WidgetUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True, required=False)
    kind = serializers.ChoiceField(choices=["chart", "table", "kpi", "text"], required=False)
    saved_query_id = serializers.UUIDField(required=False, allow_null=True)
    chart = serializers.JSONField(required=False, allow_null=True)
    layout = serializers.DictField(required=False)
    text = serializers.CharField(max_length=20_000, required=False, allow_blank=True)
