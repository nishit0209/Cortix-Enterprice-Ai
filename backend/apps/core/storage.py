"""S3/MinIO object storage helpers. Every key is prefixed by org: orgs/<org_id>/..."""

from __future__ import annotations

import functools
from typing import BinaryIO
from uuid import UUID

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from django.conf import settings

from .tenancy import require_org_id


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


def org_key(*parts: str, org_id: UUID | None = None) -> str:
    oid = org_id or require_org_id()
    return "/".join(["orgs", str(oid), *[p.strip("/") for p in parts]])


def _check_key(key: str) -> None:
    oid = str(require_org_id())
    if not key.startswith(f"orgs/{oid}/"):
        raise PermissionError("object key outside the active tenant prefix")


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
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_s)
