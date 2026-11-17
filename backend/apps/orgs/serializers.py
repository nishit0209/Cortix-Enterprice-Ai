"""Input serializers for org endpoints (responses are built by services.*_payload for exact CONTRACT shapes)."""

from __future__ import annotations

from rest_framework import serializers


class OrgCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, trim_whitespace=True)

    def validate_name(self, value: str) -> str:
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Name must have at least 2 characters.")
        return value.strip()


class OrgUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False, trim_whitespace=True)
    settings = serializers.DictField(required=False, allow_empty=True)

    def validate_name(self, value: str) -> str:
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Name must have at least 2 characters.")
        return value.strip()

    def validate(self, attrs):
        if "ai_data_policy" in self.initial_data:
            raise serializers.ValidationError(
                {"ai_data_policy": ["The AI data policy can only be changed through the consent flow."]}
            )
        return attrs


class InviteSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    role = serializers.CharField(max_length=40)


class MemberUpdateSerializer(serializers.Serializer):
    role = serializers.CharField(max_length=40, required=False)
    status = serializers.ChoiceField(choices=["active", "suspended"], required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide role and/or status.")
        return attrs


class InvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=200)
    password = serializers.CharField(required=False, allow_blank=False, trim_whitespace=False, max_length=256)
    full_name = serializers.CharField(required=False, allow_blank=True, max_length=150)


class ConsentSerializer(serializers.Serializer):
    policy = serializers.ChoiceField(choices=["schema_only", "aggregates_only", "full"])
    consent_text_version = serializers.CharField(max_length=40)
    acknowledged = serializers.BooleanField()
    password = serializers.CharField(trim_whitespace=False, max_length=256)
