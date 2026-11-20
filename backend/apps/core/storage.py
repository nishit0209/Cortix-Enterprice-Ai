"""Object storage. Every key is prefixed by org: orgs/<org_id>/... and validated against the ACTIVE tenant.

Backends (`settings.STORAGE_BACKEND`, read at call time; CONTRACT "Local runtime"):
    "s3"  S3 / MinIO (boto3)
    "fs"  a local directory: objects live at LOCAL_STORAGE_DIR/<key>. Writes are atomic (temp file in the same
          directory + os.replace). `presigned_get` returns a signed, expiring `/api/v1/files/<token>` URL served by
          `apps.core.urls_public` (TimestampSigner keyed by STORAGE_URL_SIGNING_KEY, max_age = expiry).

Public API (identical for both backends):
    org_key(*parts) -> key            put_object(key, fileobj, content_type)   put_bytes(key, data, content_type)
    get_object(key) -> bytes          open_object(key) -> binary file          download_to(key, path)
    object_exists(key) -> bool        object_size(key) -> int                  delete_object(key)
    presigned_get(key, expires_s=600, filename=None) -> url                  ensure_bucket()
    org_scope(org_id)                 ContextVar-only tenant scope for storage I/O outside a DB transaction

Backend failures raise `StorageError`, a `botocore.exceptions.BotoCoreError` subclass, so callers that already
handle `(BotoCoreError, ClientError)` behave the same with the filesystem backend; a missing object raises
`StorageObjectNotFound` (also a FileNotFoundError).
"""

from __future__ import annotations

import functools
import io
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core import signing

from .http import content_disposition
from .tenancy import require_org_id
from .tenancy.context import tenant_scope

org_scope = tenant_scope

SIGNING_SALT = "nexora.storage.download"
MAX_URL_EXPIRY_S = 7 * 24 * 3600
DOWNLOAD_PATH = "/api/v1/files/"
_ORG_KEY_RE = re.compile(r"^orgs/([0-9a-fA-F-]{36})/")
_WINDOWS_UNSAFE = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class StorageKeyError(PermissionError, ValueError):
    """An object key (or key part) that is malformed or points outside the active tenant prefix."""


class StorageError(BotoCoreError):
    """A storage backend failure (I/O error, unavailable service)."""

    fmt = "{detail}"

    def __init__(self, detail: str = "Object storage error.", **kwargs):
        super().__init__(detail=detail, **kwargs)


class StorageObjectNotFound(StorageError, FileNotFoundError):
    """The object does not exist."""


class DownloadTokenInvalid(ValueError):
    pass


class DownloadTokenExpired(DownloadTokenInvalid):
    pass


def backend() -> str:
    name = str(getattr(settings, "STORAGE_BACKEND", "") or "").lower()
    if name in ("fs", "s3"):
        return name
    return "s3" if getattr(settings, "S3_ENDPOINT_URL", "") else "fs"


# ------------------------------------------------------------------------------------------------ keys
def _check_segments(segments: list[str]) -> None:
    for seg in segments:
        if seg in ("", ".", ".."):
            raise StorageKeyError("object key contains an empty, '.' or '..' segment")
        if "\\" in seg or any(ord(ch) < 32 or ord(ch) == 127 for ch in seg):
            raise StorageKeyError("object key contains a backslash or a control character")


def org_key(*parts: str, org_id: UUID | None = None) -> str:
    """Build `orgs/<org_id>/<parts...>`. Each part may contain '/', but never '.', '..' or empty segments."""
    oid = org_id or require_org_id()
    segments: list[str] = []
    for part in parts:
        segments.extend(str(part).strip("/").split("/"))
    if not segments:
        raise StorageKeyError("object key needs at least one part below the org prefix")
    _check_segments(segments)
    return "/".join(["orgs", str(oid), *segments])


def _check_key(key: str) -> None:
    """Fail closed unless `key` is a normalised key strictly below orgs/<active org>/ (no path traversal)."""
    oid = str(require_org_id())
    prefix = f"orgs/{oid}/"
    if not isinstance(key, str) or not key.startswith(prefix):
        raise StorageKeyError("object key outside the active tenant prefix")
    _check_segments(key[len(prefix):].split("/"))


# ------------------------------------------------------------------------------------------------ s3 backend
@functools.lru_cache(maxsize=1)
def client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _s3_missing(exc: ClientError) -> bool:
    code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", ""))
    return code in ("404", "NoSuchKey", "NotFound")


# ------------------------------------------------------------------------------------------------ fs backend
def storage_root() -> Path:
    return Path(settings.LOCAL_STORAGE_DIR).resolve()


def _fs_path(key: str) -> Path:
    """The file of a validated key. Rejects names a filesystem would reinterpret (Windows drive/stream ':',
    reserved device names, trailing dots/spaces) and anything that would resolve outside the storage root."""
    _check_key(key)
    segments = key.split("/")
    for seg in segments:
        if _WINDOWS_UNSAFE.search(seg) or seg != seg.rstrip(". ") or seg.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise StorageKeyError("object key contains a character or name the filesystem backend cannot store")
    root = storage_root()
    path = root.joinpath(*segments)
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise StorageKeyError("object key resolves outside the storage root")
    return path


def _fs_write(path: Path, fileobj: BinaryIO) -> None:
    """Atomic write: a temp file next to the target, fsync, then os.replace (readers never see a partial file)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(path.parent))
    except OSError as exc:
        raise StorageError(f"Could not prepare the storage directory ({type(exc).__name__}).") from exc
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(fileobj, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise StorageError(f"Could not write the object ({type(exc).__name__}).") from exc
        raise


def _fs_open(path: Path) -> BinaryIO:
    try:
        return open(path, "rb")  # noqa: SIM115 - the caller owns the handle
    except FileNotFoundError as exc:
        raise StorageObjectNotFound("The object does not exist.") from exc
    except OSError as exc:
        raise StorageError(f"Could not read the object ({type(exc).__name__}).") from exc


# ------------------------------------------------------------------------------------------------ public API
def ensure_bucket() -> None:
    if backend() == "fs":
        try:
            storage_root().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Could not create the storage directory ({type(exc).__name__}).") from exc
        return
    c = client()
    try:
        c.head_bucket(Bucket=settings.S3_BUCKET)
    except ClientError:
        c.create_bucket(Bucket=settings.S3_BUCKET)


def put_object(key: str, fileobj: BinaryIO, content_type: str = "application/octet-stream") -> None:
    _check_key(key)
    if backend() == "fs":
        _fs_write(_fs_path(key), fileobj)
        return
    ensure_bucket()
    client().upload_fileobj(fileobj, settings.S3_BUCKET, key, ExtraArgs={"ContentType": content_type})


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    put_object(key, io.BytesIO(data), content_type)


def get_object(key: str) -> bytes:
    _check_key(key)
    if backend() == "fs":
        with _fs_open(_fs_path(key)) as fh:
            return fh.read()
    return client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"].read()


def open_object(key: str) -> BinaryIO:
    """A readable binary stream of the object (the caller closes it)."""
    _check_key(key)
    if backend() == "fs":
        return _fs_open(_fs_path(key))
    try:
        return client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"]
    except ClientError as exc:
        if _s3_missing(exc):
            raise StorageObjectNotFound("The object does not exist.") from exc
        raise


def download_to(key: str, path: str) -> None:
    _check_key(key)
    if backend() == "fs":
        with _fs_open(_fs_path(key)) as src:
            try:
                with open(path, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            except OSError as exc:
                raise StorageError(f"Could not copy the object ({type(exc).__name__}).") from exc
        return
    client().download_file(settings.S3_BUCKET, key, path)


def object_exists(key: str) -> bool:
    _check_key(key)
    if backend() == "fs":
        return _fs_path(key).is_file()
    try:
        client().head_object(Bucket=settings.S3_BUCKET, Key=key)
        return True
    except ClientError as exc:
        if _s3_missing(exc):
            return False
        raise


exists = object_exists


def object_size(key: str) -> int:
    _check_key(key)
    if backend() == "fs":
        try:
            return _fs_path(key).stat().st_size
        except FileNotFoundError as exc:
            raise StorageObjectNotFound("The object does not exist.") from exc
    return int(client().head_object(Bucket=settings.S3_BUCKET, Key=key)["ContentLength"])


def delete_object(key: str) -> None:
    _check_key(key)
    if backend() == "fs":
        try:
            _fs_path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Could not delete the object ({type(exc).__name__}).") from exc
        return
    client().delete_object(Bucket=settings.S3_BUCKET, Key=key)


def presigned_get(key: str, expires_s: int = 600, filename: str | None = None) -> str:
    _check_key(key)
    if backend() == "fs":
        token = sign_download(key, expires_s=expires_s, filename=filename)
        base = str(getattr(settings, "STORAGE_PUBLIC_BASE_URL", "") or "").rstrip("/")
        return f"{base}{DOWNLOAD_PATH}{token}"
    params = {"Bucket": settings.S3_BUCKET, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = content_disposition(filename)
    return client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_s)


# ------------------------------------------------------------------------------------------------ signed URLs
def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(key=str(settings.STORAGE_URL_SIGNING_KEY), salt=SIGNING_SALT, fallback_keys=[])


def sign_download(key: str, *, expires_s: int = 600, filename: str | None = None) -> str:
    expires = max(1, min(int(expires_s), MAX_URL_EXPIRY_S))
    payload: dict[str, Any] = {"k": key, "e": expires}
    if filename:
        payload["f"] = str(filename)[:255]
    return _signer().sign_object(payload, compress=True)


def verify_download(token: str) -> tuple[str, str | None]:
    """(key, filename) of a valid, unexpired download token. Raises DownloadTokenExpired / DownloadTokenInvalid.

    The signature is verified first (no field is trusted before), then the token's age against the expiry
    that the signed payload itself carries."""
    signer = _signer()
    try:
        payload = signer.unsign_object(token)
        expires = int(payload["e"])
        key = str(payload["k"])
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise DownloadTokenInvalid("invalid download token") from exc
    try:
        signer.unsign_object(token, max_age=min(expires, MAX_URL_EXPIRY_S))
    except signing.SignatureExpired as exc:
        raise DownloadTokenExpired("download link expired") from exc
    except signing.BadSignature as exc:
        raise DownloadTokenInvalid("invalid download token") from exc
    if not _ORG_KEY_RE.match(key):
        raise DownloadTokenInvalid("invalid download token")
    filename = payload.get("f")
    return key, (str(filename) if filename else None)


def key_org_id(key: str) -> UUID:
    match = _ORG_KEY_RE.match(key or "")
    if not match:
        raise StorageKeyError("object key has no org prefix")
    return UUID(match.group(1))


_KNOWN_TYPES = {
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".txt": "text/plain", ".json": "application/json",
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".parquet": "application/vnd.apache.parquet", ".zip": "application/zip",
}


def guess_content_type(name: str) -> str:
    """Stable types for the formats NEXORA serves (the OS registry is not consulted for them, e.g. Windows maps
    .csv to Excel); anything else falls back to mimetypes, then application/octet-stream."""
    ext = os.path.splitext(str(name or ""))[1].lower()
    return _KNOWN_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
