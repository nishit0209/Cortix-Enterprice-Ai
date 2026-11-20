"""GET /api/v1/files/<token>: signed, expiring downloads for the filesystem storage backend.

The token (see `apps.core.storage.sign_download`) IS the capability, exactly like an S3 presigned URL: it is
minted only by code that already authorised the caller for that object, it names one key, and it expires. No
session is needed (the URL may be opened in a new tab or by a download manager).

    200  streaming FileResponse, RFC 6266 Content-Disposition (attachment), nosniff, no-store
    404  bad / tampered token, or the object no longer exists (nothing is revealed)
    410  the link expired (`download_expired`)
"""

from __future__ import annotations

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET

from . import storage
from .http import content_disposition


def _problem(status: int, code: str, detail: str) -> JsonResponse:
    resp = JsonResponse({"type": "about:blank", "status": status, "code": code, "detail": detail}, status=status)
    resp["Content-Type"] = "application/problem+json"
    resp["Cache-Control"] = "no-store"
    return resp


@require_GET
def download(request, token: str):
    if storage.backend() != "fs":
        return _problem(404, "not_found", "Not found.")
    try:
        key, filename = storage.verify_download(token)
    except storage.DownloadTokenExpired:
        return _problem(410, "download_expired", "This download link has expired. Request a new one.")
    except storage.DownloadTokenInvalid:
        return _problem(404, "not_found", "Not found.")
    try:
        with storage.org_scope(storage.key_org_id(key)):
            handle = storage.open_object(key)
    except (storage.StorageKeyError, storage.StorageObjectNotFound):
        return _problem(404, "not_found", "Not found.")
    except storage.StorageError:
        return _problem(503, "storage_unavailable", "The file is temporarily unavailable.")
    name = filename or key.rsplit("/", 1)[-1]
    resp = FileResponse(handle, content_type=storage.guess_content_type(name))
    resp["Content-Disposition"] = content_disposition(name)
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    resp["Referrer-Policy"] = "no-referrer"
    return resp
