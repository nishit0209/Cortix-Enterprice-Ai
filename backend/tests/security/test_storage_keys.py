"""Object-storage key prefix enforcement (apps.core.storage): every key must live under orgs/<active org>/."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from apps.core import storage
from apps.core.tenancy import TenantContextMissing, current_org

ORG_A = uuid.UUID("018f0000-0000-7000-8000-00000000000a")
ORG_B = uuid.UUID("018f0000-0000-7000-8000-00000000000b")


@contextmanager
def active(org_id):
    """ContextVar-only tenant (storage never touches the DB, so no transaction is needed)."""
    token = current_org.set(org_id)
    try:
        yield
    finally:
        current_org.reset(token)


class _RecordingS3:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "get_object":
                import io

                return {"Body": io.BytesIO(b"data")}
            if name == "generate_presigned_url":
                return "https://minio.local/signed"
            return None

        return call


@pytest.fixture
def s3(monkeypatch):
    fake = _RecordingS3()
    monkeypatch.setattr(storage, "client", lambda: fake)
    return fake


def test_org_key_builds_the_tenant_prefix():
    with active(ORG_A):
        assert storage.org_key("uploads", "f.csv") == f"orgs/{ORG_A}/uploads/f.csv"
        assert storage.org_key("/uploads/", "/f.csv") == f"orgs/{ORG_A}/uploads/f.csv"
    assert storage.org_key("x", org_id=ORG_B) == f"orgs/{ORG_B}/x"


def test_org_key_requires_context():
    with pytest.raises(TenantContextMissing):
        storage.org_key("x")


def test_check_key_accepts_own_prefix_only():
    with active(ORG_A):
        storage._check_key(f"orgs/{ORG_A}/uploads/f.csv")
        for bad in (
            f"orgs/{ORG_B}/uploads/f.csv",
            f"orgs/{ORG_A}",  # no trailing slash: not inside the prefix
            f"orgs/{ORG_A}x/f.csv",  # prefix look-alike
            f"/orgs/{ORG_A}/f.csv",
            f"ORGS/{ORG_A}/f.csv",
            "orgs//f.csv",
            "",
        ):
            with pytest.raises(PermissionError):
                storage._check_key(bad)


def test_check_key_fails_closed_without_context():
    with pytest.raises(TenantContextMissing):
        storage._check_key(f"orgs/{ORG_A}/f.csv")


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT core-storage-traversal: _check_key() only tests startswith('orgs/<org>/'), so keys with '..' "
    "segments (orgs/<A>/../<B>/f) pass. S3 treats keys literally, but MinIO/gateways and any filesystem-backed "
    "mirror or download_to() target path normalise them. org_key() also accepts '..' parts.",
)
def test_check_key_rejects_path_traversal_segments():
    with active(ORG_A):
        for bad in (
            f"orgs/{ORG_A}/../{ORG_B}/secret.csv",
            f"orgs/{ORG_A}/uploads/../../{ORG_B}/f",
            f"orgs/{ORG_A}/./../{ORG_B}/f",
        ):
            with pytest.raises(PermissionError):
                storage._check_key(bad)
        with pytest.raises((PermissionError, ValueError)):
            storage.org_key("..", str(ORG_B), "f.csv")


@pytest.mark.parametrize(
    ("fn", "args"),
    [
        ("put_bytes", (b"x",)),
        ("get_object", ()),
        ("delete_object", ()),
        ("presigned_get", ()),
        ("download_to", ("ignored-path",)),
    ],
)
def test_every_operation_checks_the_prefix_before_calling_s3(s3, fn, args):
    with active(ORG_A):
        with pytest.raises(PermissionError):
            getattr(storage, fn)(f"orgs/{ORG_B}/f.csv", *args)
    assert s3.calls == []


def test_own_prefix_operations_reach_s3(s3, settings):
    key = f"orgs/{ORG_A}/uploads/f.csv"
    with active(ORG_A):
        storage.put_bytes(key, b"a,b\n1,2\n", "text/csv")
        assert storage.get_object(key) == b"data"
        assert storage.presigned_get(key, filename="report.csv") == "https://minio.local/signed"
    names = [c[0] for c in s3.calls]
    assert names == ["head_bucket", "upload_fileobj", "get_object", "generate_presigned_url"]
    assert s3.calls[1][1][2] == key  # (fileobj, bucket, key)
    presign = s3.calls[3]
    assert presign[2]["ExpiresIn"] == 600


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT core-storage-content-disposition: presigned_get() interpolates `filename` into "
    "ResponseContentDisposition without escaping, so a user-controlled name containing quotes/CR/LF/';' can "
    "inject header parameters into the signed response.",
)
def test_presigned_filename_is_sanitised(s3):
    with active(ORG_A):
        storage.presigned_get(f"orgs/{ORG_A}/f.csv", filename='evil"; filename*=UTF-8\'\'x.html\r\nX-Test: 1')
    disposition = s3.calls[-1][2]["Params"]["ResponseContentDisposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert disposition.count('"') <= 2
    assert "filename*=UTF-8''x.html" not in disposition
