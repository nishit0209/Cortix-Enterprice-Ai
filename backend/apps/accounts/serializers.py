from __future__ import annotations

from zoneinfo import available_timezones

from rest_framework import serializers


class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(trim_whitespace=False, max_length=256)
    full_name = serializers.CharField(max_length=150, trim_whitespace=True)
    org_name = serializers.CharField(max_length=120, trim_whitespace=True)

    def validate_org_name(self, value: str) -> str:
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Organization name must have at least 2 characters.")
        return value.strip()


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(trim_whitespace=False, max_length=256)
    otp = serializers.RegexField(r"^\d{6}$", required=False)


class MeUpdateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    timezone = serializers.CharField(max_length=64, required=False)

    def validate_timezone(self, value: str) -> str:
        if value not in available_timezones():
            raise serializers.ValidationError("Unknown time zone.")
        return value


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False, max_length=256)
    new_password = serializers.CharField(trim_whitespace=False, max_length=256)


class OtpSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^\d{6}$")


class MfaDisableSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False, max_length=256)
    code = serializers.RegexField(r"^\d{6}$")
