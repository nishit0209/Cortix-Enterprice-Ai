"""RFC 7807 problem+json errors with stable `code`s (see docs/API_SPECIFICATION.md §3)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.views import exception_handler

from .middleware import request_id_var


class NexoraError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "error"
    default_detail = "Request failed."

    def __init__(self, detail: str | None = None, *, code: str | None = None, extra: dict | None = None):
        super().__init__(detail or self.default_detail, code or self.default_code)
        self.extra = extra or {}


class Conflict(NexoraError):
    status_code = 409
    default_code = "conflict"
    default_detail = "Conflict."


class Unprocessable(NexoraError):
    status_code = 422
    default_code = "unprocessable"
    default_detail = "The request could not be processed."


class Forbidden(NexoraError):
    status_code = 403
    default_code = "forbidden"
    default_detail = "You do not have permission to perform this action."


class OrgSuspended(NexoraError):
    """The caller is a member, but a platform admin suspended the org (403 `org_suspended`)."""

    status_code = 403
    default_code = "org_suspended"
    default_detail = "This organization is suspended. Contact your platform administrator."


class AIPolicyConsentRequired(NexoraError):
    status_code = 400
    default_code = "ai_policy_consent_required"
    default_detail = "This AI data policy requires an acknowledged consent for the current consent text."


class AIPolicyTierForbidden(NexoraError):
    status_code = 403
    default_code = "ai_policy_tier_forbidden"
    default_detail = "This AI data policy is not allowed while the Gemini free tier is configured."


class AIPolicyBlocked(NexoraError):
    status_code = 403
    default_code = "ai_policy_blocked"
    default_detail = "Your organization's AI data policy does not allow sending this data to Gemini."


class AIQuotaExhausted(NexoraError):
    status_code = 429
    default_code = "ai_quota_exhausted"
    default_detail = "The AI quota is exhausted. It resets later."


class AIUnavailable(NexoraError):
    status_code = 503
    default_code = "ai_unavailable"
    default_detail = "AI is temporarily unavailable."


class ReauthFailed(NexoraError):
    """Password re-authentication failed (400 `reauth_failed`, `errors: [{field: "password"}]`)."""

    status_code = 400
    default_code = "reauth_failed"
    default_detail = "The password is incorrect."

    def __init__(self, detail: str | None = None, *, code: str | None = None, extra: dict | None = None):
        message = detail or self.default_detail
        extra = {"errors": [{"field": "password", "code": "reauth_failed", "message": message}], **(extra or {})}
        super().__init__(message, code=code, extra=extra)


class SqlRejected(NexoraError):
    status_code = 422
    default_code = "sql_rejected"
    default_detail = "The SQL was rejected by the security guard."


def problem_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None
    detail = response.data
    code = getattr(exc, "default_code", "error")
    if isinstance(exc, NexoraError):
        code = exc.get_codes() if isinstance(exc.get_codes(), str) else code
    elif isinstance(exc, APIException):
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else ("invalid" if isinstance(exc, ValidationError) else code)
    if code == "throttled":
        code = "rate_limited"  # stable code from API_SPECIFICATION.md §2
    body = {
        "type": "about:blank",
        "title": response.status_text if hasattr(response, "status_text") else "Error",
        "status": response.status_code,
        "code": code,
        "detail": detail.get("detail") if isinstance(detail, dict) and "detail" in detail else None,
        "request_id": request_id_var.get(),
    }
    if isinstance(exc, ValidationError):
        body["detail"] = "Validation failed."
        body["errors"] = _flatten(detail)
    if isinstance(exc, NexoraError) and exc.extra:
        body.update(exc.extra)
    response.data = body
    # `content_type` (not just the header): DRF re-sets the header from it when the response is rendered.
    response.content_type = "application/problem+json"
    response["Content-Type"] = "application/problem+json"
    return response


def _flatten(data, prefix: str = "") -> list[dict]:
    out: list[dict] = []
    if isinstance(data, dict):
        for k, v in data.items():
            out.extend(_flatten(v, f"{prefix}{k}" if not prefix else f"{prefix}.{k}"))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                out.extend(_flatten(item, prefix))
            else:
                out.append({"field": prefix or None, "code": getattr(item, "code", "invalid"), "message": str(item)})
    else:
        out.append({"field": prefix or None, "code": getattr(data, "code", "invalid"), "message": str(data)})
    return out
