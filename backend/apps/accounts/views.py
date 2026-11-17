"""Authentication & profile endpoints (/api/v1/auth/*, /api/v1/me). Session cookie only: API keys can
never reach these (API_SPECIFICATION.md §1)."""

from __future__ import annotations

import base64

from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.auth import CsrfEnforcedSessionAuthentication, enforce_csrf
from apps.core.exceptions import Conflict, NexoraError
from apps.core.tenancy import tenant_context
from apps.core.tenancy.views import resolve_membership
from apps.orgs import services as org_services

from . import services
from .models import User
from .serializers import (
    LoginSerializer,
    MeUpdateSerializer,
    MfaDisableSerializer,
    OtpSerializer,
    PasswordChangeSerializer,
    SignupSerializer,
)

GENERIC_LOGIN_ERROR = "Invalid email or password."


class InvalidCredentials(NexoraError):
    status_code = 400
    default_code = "invalid_credentials"
    default_detail = GENERIC_LOGIN_ERROR


class MfaRequired(NexoraError):
    status_code = 400
    default_code = "mfa_required"
    default_detail = "Enter the 6-digit code from your authenticator app."


class LoginLocked(Throttled):
    default_code = "login_locked"

    def __init__(self, wait: int):
        super().__init__(wait=wait, detail="Too many failed sign-in attempts. Try again later.", code="login_locked")


def _password_errors(password: str, user) -> None:
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise ValidationError({"password": list(exc.messages)}) from exc


def _session_login(request, user) -> None:
    """Session login; Django rotates the session key and the CSRF token."""
    login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")


class SessionView(APIView):
    authentication_classes = [CsrfEnforcedSessionAuthentication]
    permission_classes = [IsAuthenticated]


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """GET /auth/csrf -> 204 and sets the `csrftoken` cookie."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(status=status.HTTP_204_NO_CONTENT)


class SignupView(APIView):
    """POST {email, password, full_name, org_name} -> 201 {user, org} + session.

    The org is always created with ai_data_policy=synthetic_only (the body cannot set it)."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        enforce_csrf(request)
        ser = SignupSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        email = data["email"].strip().lower()
        candidate = User(email=email, full_name=data["full_name"])
        _password_errors(data["password"], candidate)
        taken = Conflict("An account with this email already exists.", code="email_taken")
        if User.objects.filter(email__iexact=email).exists():
            raise taken
        try:
            with transaction.atomic():
                user = User.objects.create_user(email, data["password"], full_name=data["full_name"])
                org = org_services.create_organization(name=data["org_name"], owner=user, request=request)
        except IntegrityError as exc:  # concurrent signup with the same email
            raise taken from exc
        _session_login(request, user)
        info = resolve_membership(user.pk, org.slug)
        with tenant_context(org.id, info=info):
            org_body = org_services.org_payload(org, info)
        return Response({"user": services.user_payload(user), "org": org_body}, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    """POST {email, password, otp?} -> 200 {user}. Generic errors; per account+IP lockout with backoff."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        enforce_csrf(request)
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email, password = ser.validated_data["email"].strip().lower(), ser.validated_data["password"]
        ip = services.client_ip(request)
        remaining = services.login_lock_remaining(email, ip)
        if remaining:
            raise LoginLocked(remaining)
        user = authenticate(request._request, username=email, password=password)
        if user is None:
            lock = services.register_login_failure(email, ip)
            if lock:
                raise LoginLocked(lock)
            raise InvalidCredentials()
        device = None
        if user.mfa_enabled:
            from . import mfa

            otp = ser.validated_data.get("otp")
            if not otp:
                raise MfaRequired(extra={"mfa_required": True})
            device = mfa.verify_login_token(user, otp)
            if device is None:
                lock = services.register_login_failure(email, ip)
                if lock:
                    raise LoginLocked(lock)
                raise NexoraError("The authentication code is invalid.", code="mfa_invalid")
        services.reset_login_failures(email, ip)
        _session_login(request, user)
        if device is not None:
            from django_otp import login as otp_login

            otp_login(request._request, device)
        return Response({"user": services.user_payload(user)})


class LogoutView(APIView):
    authentication_classes = [CsrfEnforcedSessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        logout(request._request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(SessionView):
    """GET -> {user, orgs}; PATCH {full_name?, timezone?} -> same shape."""

    def _body(self, user):
        return {"user": services.user_payload(user), "orgs": org_services.user_orgs(user.pk)}

    def get(self, request):
        return Response(self._body(request.user))

    def patch(self, request):
        ser = MeUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        user = request.user
        fields = []
        for name in ("full_name", "timezone"):
            if name in ser.validated_data:
                setattr(user, name, ser.validated_data[name].strip() if name == "full_name" else ser.validated_data[name])
                fields.append(name)
        if fields:
            user.save(update_fields=[*fields, "updated_at"])
        return Response(self._body(user))


class PasswordChangeView(SessionView):
    """POST {current_password, new_password} -> 204 (session kept, key rotated)."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        ser = PasswordChangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(ser.validated_data["current_password"]):
            raise ValidationError({"current_password": ["The current password is incorrect."]})
        new = ser.validated_data["new_password"]
        if new == ser.validated_data["current_password"]:
            raise ValidationError({"new_password": ["The new password must differ from the current one."]})
        _password_errors(new, user)
        user.set_password(new)
        user.save(update_fields=["password", "updated_at"])
        update_session_auth_hash(request._request, user)
        request._request.session.cycle_key()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- TOTP MFA (django-otp) --------------------------------------------------------------------------------
class TotpSetupView(SessionView):
    """POST -> {otpauth_url, secret}: a new unconfirmed TOTP device (replaces any unconfirmed one)."""

    def post(self, request):
        from . import mfa

        if request.user.mfa_enabled:
            raise Conflict("MFA is already enabled.", code="mfa_already_enabled")
        device = mfa.start_setup(request.user)
        return Response(
            {"otpauth_url": device.config_url, "secret": base64.b32encode(device.bin_key).decode().rstrip("=")},
            status=status.HTTP_201_CREATED,
        )


class TotpVerifyView(SessionView):
    """POST {code} -> confirms the pending device and enables MFA."""

    def post(self, request):
        from . import mfa

        ser = OtpSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        device = mfa.confirm_setup(request.user, ser.validated_data["code"])
        if device is None:
            raise NexoraError("The authentication code is invalid.", code="mfa_invalid")
        from django_otp import login as otp_login

        otp_login(request._request, device)
        return Response({"user": services.user_payload(request.user)})


class TotpDisableView(SessionView):
    """POST {password, code} -> disables MFA (re-auth with password AND a current code)."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        from . import mfa

        ser = MfaDisableSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not request.user.check_password(ser.validated_data["password"]):
            raise ValidationError({"password": ["The password is incorrect."]})
        if not request.user.mfa_enabled:
            raise Conflict("MFA is not enabled.", code="mfa_not_enabled")
        if mfa.verify_login_token(request.user, ser.validated_data["code"]) is None:
            raise NexoraError("The authentication code is invalid.", code="mfa_invalid")
        mfa.disable(request.user)
        return Response({"user": services.user_payload(request.user)})
