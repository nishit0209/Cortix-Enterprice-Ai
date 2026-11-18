"""S3/MinIO object storage helpers. Every key is prefixed by org: orgs/<org_id>/..."""

from __future__ import annotations

import functools
from typing import BinaryIO
from uuid import UUID

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from django.conf import settings

from .http import content_disposition
from .tenancy import require_org_id


class StorageKeyError(PermissionError, ValueError):
    """An object key (or key part) that is malformed or points outside the active tenant prefix."""


@functools.lru_cache(maxsize=1)
def client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def ensure_bucket() -> None:
    c = client()
    try:
        c.head_bucket(Bucket=settings.S3_BUCKET)
    except ClientError:
        c.create_bucket(Bucket=settings.S3_BUCKET)


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


def put_object(key: str, fileobj: BinaryIO, content_type: str = "application/octet-stream") -> None:
    _check_key(key)
    ensure_bucket()
    client().upload_fileobj(fileobj, settings.S3_BUCKET, key, ExtraArgs={"ContentType": content_type})


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    import io

    put_object(key, io.BytesIO(data), content_type)


def get_object(key: str) -> bytes:
    _check_key(key)
    return client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"].read()


def download_to(key: str, path: str) -> None:
    _check_key(key)
    client().download_file(settings.S3_BUCKET, key, path)


def delete_object(key: str) -> None:
    _check_key(key)
    client().delete_object(Bucket=settings.S3_BUCKET, Key=key)


def presigned_get(key: str, expires_s: int = 600, filename: str | None = None) -> str:
    _check_key(key)
    params = {"Bucket": settings.S3_BUCKET, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = content_disposition(filename)
    return client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_s)
