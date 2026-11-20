"""The ONLY module that imports the Gemini SDK (`google-genai`). Everything else goes through
`apps.ai.services.gemini()`.

`GenaiTransport` adapts the SDK to the `apps.ai.transport.Transport` protocol: request building
(system instruction, JSON mode + response_schema, temperature, max tokens, per-request timeout),
usage_metadata extraction and error classification (429 per-minute vs per-day, RetryInfo.retryDelay,
5xx, timeouts). Retries, quotas, policy and logging live in `apps.ai.engine`.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .transport import GenerateRequest, RawResult, StreamChunk, TransportError, Usage

log = logging.getLogger("nexora.ai.gemini")

_DURATION_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*s\s*$")


def parse_retry_delay(value: Any) -> float | None:
    """'13s' / '1.5s' / {'seconds': 13} -> seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        try:
            return float(value.get("seconds", 0)) + float(value.get("nanos", 0)) / 1e9
        except (TypeError, ValueError):
            return None
    m = _DURATION_RE.match(str(value))
    return float(m.group(1)) if m else None


def classify_api_error(code: int | None, status: str | None, details: Any, message: str = "") -> TransportError:
    """Map a Gemini API error (HTTP code + google.rpc details) to a TransportError."""
    retry_delay = None
    per_day = False
    per_minute = False
    for d in details if isinstance(details, list) else []:
        if not isinstance(d, dict):
            continue
        typ = str(d.get("@type", ""))
        if typ.endswith("RetryInfo"):
            retry_delay = parse_retry_delay(d.get("retryDelay"))
        if typ.endswith("QuotaFailure"):
            for v in d.get("violations", []) or []:
                qid = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}".lower()
                if "perday" in qid or "per_day" in qid or "requests_per_day" in qid:
                    per_day = True
                if "perminute" in qid or "per_minute" in qid:
                    per_minute = True
    text = f"{status or ''} {message}".lower()
    if code == 429 or (status or "").upper() == "RESOURCE_EXHAUSTED":
        if per_day or ("per day" in text and not per_minute):
            return TransportError("rate_day", message, retry_delay_s=retry_delay, http_status=429)
        return TransportError("rate_minute", message, retry_delay_s=retry_delay, http_status=429)
    if code in (500, 502, 503, 504) or (status or "").upper() in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
        kind = "timeout" if code == 504 or (status or "").upper() == "DEADLINE_EXCEEDED" else "unavailable"
        return TransportError(kind, message, retry_delay_s=retry_delay, http_status=code)
    if code in (401, 403):
        return TransportError("auth", message, http_status=code)
    if code in (400, 404, 409, 422):
        return TransportError("bad_request", message, http_status=code)
    return TransportError("unknown", message, http_status=code)


def _usage(meta: Any) -> Usage:
    if meta is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
        output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0)
        + int(getattr(meta, "thoughts_token_count", 0) or 0),
        cached_tokens=int(getattr(meta, "cached_content_token_count", 0) or 0),
    )


def _finish_reason(resp: Any) -> str | None:
    cands = getattr(resp, "candidates", None) or []
    if not cands:
        return None
    fr = getattr(cands[0], "finish_reason", None)
    if fr is None:
        return None
    return str(getattr(fr, "value", fr) or "") or None


def _block_reason(resp: Any) -> str | None:
    fb = getattr(resp, "prompt_feedback", None)
    br = getattr(fb, "block_reason", None) if fb is not None else None
    if br is None:
        return None
    return str(getattr(br, "value", br) or "") or None


def _safe_text(resp: Any) -> str | None:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 - SDK raises when there is no text part (e.g. safety)
        return None


class GenaiTransport:
    """google-genai backed transport. The client is created lazily (and only when a key is configured)."""

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                from google import genai
                from google.genai import types

                tier = str(getattr(settings, "GEMINI_TIER", "free")).lower()
                http_options = types.HttpOptions(timeout=int(float(settings.GEMINI_REQUEST_TIMEOUT_S) * 1000))
                if tier == "vertex":
                    project = getattr(settings, "GEMINI_VERTEX_PROJECT", "")
                    location = getattr(settings, "GEMINI_VERTEX_LOCATION", "")
                    if not project or not location:
                        raise ImproperlyConfigured("GEMINI_VERTEX_PROJECT/GEMINI_VERTEX_LOCATION are required")
                    self._client = genai.Client(vertexai=True, project=project, location=location,
                                                http_options=http_options)
                else:
                    api_key = settings.GEMINI_API_KEY
                    if not api_key:
                        raise ImproperlyConfigured("GEMINI_API_KEY is not set")
                    self._client = genai.Client(api_key=api_key, http_options=http_options)
        return self._client

    @staticmethod
    def _config(request: GenerateRequest):
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": request.system or None,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "http_options": types.HttpOptions(timeout=int(request.timeout_s * 1000)),
        }
        if request.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = request.response_schema
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _translate(exc: Exception) -> TransportError:
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            details = None
            body = getattr(exc, "details", None)
            if isinstance(body, dict):
                details = (body.get("error") or {}).get("details") if "error" in body else body.get("details")
            return classify_api_error(getattr(exc, "code", None), getattr(exc, "status", None), details,
                                      getattr(exc, "message", None) or str(exc))
        name = type(exc).__name__.lower()
        if "timeout" in name:
            return TransportError("timeout", str(exc))
        if "connect" in name or "network" in name or "remoteprotocol" in name:
            return TransportError("unavailable", str(exc))
        return TransportError("unknown", str(exc))

    def generate(self, request: GenerateRequest) -> RawResult:
        client = self._get_client()
        try:
            resp = client.models.generate_content(model=request.model, contents=request.prompt,
                                                  config=self._config(request))
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return RawResult(text=_safe_text(resp), usage=_usage(getattr(resp, "usage_metadata", None)),
                         finish_reason=_finish_reason(resp), block_reason=_block_reason(resp))

    def stream(self, request: GenerateRequest) -> Iterator[StreamChunk]:
        client = self._get_client()
        try:
            iterator = client.models.generate_content_stream(model=request.model, contents=request.prompt,
                                                             config=self._config(request))
            for chunk in iterator:
                meta = getattr(chunk, "usage_metadata", None)
                yield StreamChunk(text=_safe_text(chunk) or "", usage=_usage(meta) if meta is not None else None,
                                  finish_reason=_finish_reason(chunk), block_reason=_block_reason(chunk))
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    def list_models(self) -> list[dict]:
        """Model catalogue visible to the configured credentials (metadata only; NO generation call).

        Returns [{"id": "gemini-...", "display_name", "actions": [...], "input_token_limit",
        "output_token_limit"}]; ids have the "models/" (or Vertex "publishers/google/models/") prefix stripped."""
        client = self._get_client()
        out: list[dict] = []
        try:
            for m in client.models.list():
                name = str(getattr(m, "name", "") or "")
                out.append({
                    "id": name.rsplit("/", 1)[-1],
                    "display_name": getattr(m, "display_name", None),
                    "actions": list(getattr(m, "supported_actions", None) or []),
                    "input_token_limit": getattr(m, "input_token_limit", None),
                    "output_token_limit": getattr(m, "output_token_limit", None),
                })
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return out
