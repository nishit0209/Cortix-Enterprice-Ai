"""POST /api/v1/hooks/<trigger_id>: inbound webhooks for workflow triggers (CONTRACT "W5 additions / Triggers").

No session and no CSRF: the request is authenticated by an HMAC-SHA256 signature with the trigger's secret plus a
timestamp header (5 minute window), verified by the receiver that apps.automation registers
(`apps.core.registry.register_webhook_receiver`). Errors are RFC 7807 problem+json.
"""

from __future__ import annotations

import json
from uuid import UUID

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .middleware import request_id_var
from .registry import webhook_receiver

_TITLES = {400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict", 413: "Payload Too Large",
           500: "Internal Server Error"}


def _problem(status: int, code: str, detail: str) -> HttpResponse:
    body = {"type": "about:blank", "title": _TITLES.get(status, "Error"), "status": status, "code": code,
            "detail": detail, "request_id": request_id_var.get()}
    return HttpResponse(json.dumps(body), status=status, content_type="application/problem+json")


@csrf_exempt
@require_POST
def webhook(request, trigger_id: UUID):
    receiver = webhook_receiver()
    if receiver is None:
        return _problem(404, "not_found", "Not found.")
    status, body = receiver(trigger_id, body=request.body, headers=request.headers)
    if status >= 400:
        return _problem(status, str(body.get("code") or "error"), str(body.get("detail") or ""))
    return HttpResponse(json.dumps(body), status=status, content_type="application/json")
