"""Filesystem storage backend + signed download URLs (no database): key validation / traversal, atomic writes,
signing, expiry, tampering, the streaming download view, and the ContextVar-only tenant scope."""

from __future__ import annotations

import io
import os
import time
from uuid import uuid4

import pytest
from botocore.exceptions import BotoCoreError
from django.core import signing
from django.test import RequestFactory

from apps.core import files, storage
from apps.core.tenancy import TenantContextMissing, current_org, tenant_scope
from apps.core.tenancy.context import TenantMismatch

ORG_A = uuid4()
ORG_B = uuid4()


@pytest.fixture
def fs(settings, tmp_path):
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = tmp_path / "storage"
    settings.STORAGE_URL_SIGNING_KEY = "unit-test-signing-key"
    return tmp_path / "storage"


def _files_under(path) -> list[str]:
    return sorted(str(p.relative_to(path)).replace("\\", "/") for p in path.rglob("*") if p.is_file())


# ------------------------------------------------------------------------------------------------ round trip
def test_round_trip_through_the_public_api(fs, tmp_path):
    with tenant_scope(ORG_A):
        key = storage.org_key("uploads", "u1", "sales.csv")
        storage.put_bytes(key, b"a,b\n1,2\n", "text/csv")
        assert storage.get_object(key) == b"a,b\n1,2\n"
        assert storage.object_exists(key) and storage.exists(key)
        assert storage.object_size(key) == 8
        with storage.open_object(key) as fh:
            assert fh.read() == b"a,b\n1,2\n"
        target = tmp_path / "copy.csv"
        storage.download_to(key, str(target))
        assert target.read_bytes() == b"a,b\n1,2\n"
        storage.put_object(key, io.BytesIO(b"replaced"))
        assert storage.get_object(key) == b"replaced"
        storage.delete_object(key)
        storage.delete_object(key)  # idempotent
        assert not storage.object_exists(key)
    assert (fs / "orgs" / str(ORG_A) / "uploads" / "u1").is_dir()


def test_missing_objects_raise_a_boto_compatible_not_found(fs):
    with tenant_scope(ORG_A):
        key = storage.org_key("nope.csv")
        for call in (storage.get_object, storage.open_object, storage.object_size):
            with pytest.raises(storage.StorageObjectNotFound) as exc:
                call(key)
            assert isinstance(exc.value, BotoCoreError) and isinstance(exc.value, FileNotFoundError)


def test_backend_selection(settings):
    settings.STORAGE_BACKEND = "s3"
    assert storage.backend() == "s3"
    settings.STORAGE_BACKEND = ""
    settings.S3_ENDPOINT_URL = ""
    assert storage.backend() == "fs"
    settings.S3_ENDPOINT_URL = "http://minio:9000"
    assert storage.backend() == "s3"


# ------------------------------------------------------------------------------------------------ traversal
@pytest.mark.parametrize("bad_suffix", [
    "../escape.csv", "a/../../escape.csv", "./a.csv", "a//b.csv", "a\\..\\b.csv", "a/b\x00.csv",
    "c:/windows/win.ini", "data.csv:secret", "CON", "nul.txt", "trailing.", "space ",
])
def test_traversal_and_filesystem_tricks_are_rejected(fs, bad_suffix):
    with tenant_scope(ORG_A):
        key = f"orgs/{ORG_A}/{bad_suffix}"
        with pytest.raises(storage.StorageKeyError):
            storage.put_bytes(key, b"x")
        with pytest.raises(storage.StorageKeyError):
            storage.get_object(key)
    assert not (fs.parent / "escape.csv").exists()
    assert _files_under(fs) == [] if fs.exists() else True


def test_foreign_and_absolute_keys_are_rejected(fs, tmp_path):
    with tenant_scope(ORG_A):
        for key in (f"orgs/{ORG_B}/x.csv", "/etc/passwd", str(tmp_path / "x.csv"), "orgs/x.csv", ""):
            with pytest.raises(storage.StorageKeyError):
                storage.put_bytes(key, b"x")


def test_no_tenant_means_no_storage(fs):
    with pytest.raises(TenantContextMissing):
        storage.put_bytes(f"orgs/{ORG_A}/x.csv", b"x")


def test_symlinked_directory_cannot_escape_the_root(fs, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link_parent = fs / "orgs" / str(ORG_A)
    link_parent.mkdir(parents=True)
    try:
        os.symlink(outside, link_parent / "evil", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this user")
    with tenant_scope(ORG_A), pytest.raises(storage.StorageKeyError):
        storage.put_bytes(f"orgs/{ORG_A}/evil/x.csv", b"x")
    assert list(outside.iterdir()) == []


# ------------------------------------------------------------------------------------------------ atomicity
class _FailingReader(io.RawIOBase):
    """Yields one chunk, then fails like a dropped upload."""

    def __init__(self):
        self.calls = 0

    def readable(self):
        return True

    def readinto(self, b):
        self.calls += 1
        if self.calls == 1:
            b[:4] = b"NEW!"
            return 4
        raise OSError("connection reset")


def test_writes_are_atomic(fs):
    with tenant_scope(ORG_A):
        key = storage.org_key("reports", "r.csv")
        storage.put_bytes(key, b"old complete content")
        with pytest.raises(storage.StorageError):
            storage.put_object(key, _FailingReader())
        assert storage.get_object(key) == b"old complete content"  # never a partial file
    assert _files_under(fs) == [f"orgs/{ORG_A}/reports/r.csv"]  # the temp file was removed


def test_write_goes_through_a_temp_file_and_os_replace(fs, monkeypatch):
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(storage.os, "replace", lambda src, dst: (replaced.append((src, dst)), real_replace(src, dst)))
    with tenant_scope(ORG_A):
        key = storage.org_key("a.bin")
        storage.put_bytes(key, b"123")
    (src, dst), = replaced
    assert os.path.dirname(src) == os.path.dirname(dst) and os.path.basename(src).startswith(".tmp-")


# ------------------------------------------------------------------------------------------------ signing
def test_presigned_url_is_a_signed_api_path(fs):
    with tenant_scope(ORG_A):
        key = storage.org_key("exports", "q3 report.csv")
        storage.put_bytes(key, b"x")
        url = storage.presigned_get(key, expires_s=120, filename="Q3 \"report\";.csv")
    assert url.startswith(storage.DOWNLOAD_PATH)
    token = url[len(storage.DOWNLOAD_PATH):]
    assert "/" not in token
    assert storage.verify_download(token) == (key, "Q3 \"report\";.csv")


def test_tampered_tokens_are_invalid(fs):
    token = storage.sign_download(f"orgs/{ORG_A}/a.csv", expires_s=60)
    body, _, sig = token.rpartition(":")
    forged = storage.sign_download(f"orgs/{ORG_B}/a.csv", expires_s=60)
    for bad in (token + "x", body + ":" + sig[::-1], token.replace(token[2], "Z" if token[2] != "Z" else "Y", 1),
                "garbage", forged.rsplit(":", 1)[0] + ":" + sig):
        with pytest.raises(storage.DownloadTokenInvalid):
            storage.verify_download(bad)


def test_tokens_signed_with_another_key_are_invalid(fs, settings):
    token = storage.sign_download(f"orgs/{ORG_A}/a.csv", expires_s=60)
    settings.STORAGE_URL_SIGNING_KEY = "rotated"
    with pytest.raises(storage.DownloadTokenInvalid):
        storage.verify_download(token)


def test_tokens_expire_after_their_own_max_age(fs, monkeypatch):
    now = time.time()
    monkeypatch.setattr(signing.time, "time", lambda: now)
    token = storage.sign_download(f"orgs/{ORG_A}/a.csv", expires_s=60)
    monkeypatch.setattr(signing.time, "time", lambda: now + 59)
    assert storage.verify_download(token)[0] == f"orgs/{ORG_A}/a.csv"
    monkeypatch.setattr(signing.time, "time", lambda: now + 61)
    with pytest.raises(storage.DownloadTokenExpired):
        storage.verify_download(token)


def test_expiry_is_capped(fs, monkeypatch):
    now = time.time()
    monkeypatch.setattr(signing.time, "time", lambda: now)
    token = storage.sign_download(f"orgs/{ORG_A}/a.csv", expires_s=10 ** 9)
    monkeypatch.setattr(signing.time, "time", lambda: now + storage.MAX_URL_EXPIRY_S + 1)
    with pytest.raises(storage.DownloadTokenExpired):
        storage.verify_download(token)


def test_a_signed_key_outside_an_org_prefix_is_refused(fs):
    token = storage._signer().sign_object({"k": "../../etc/passwd", "e": 60}, compress=True)
    with pytest.raises(storage.DownloadTokenInvalid):
        storage.verify_download(token)


# ------------------------------------------------------------------------------------------------ download view
def _get(token):
    return files.download(RequestFactory().get(f"/api/v1/files/{token}"), token)


def test_download_view_streams_with_rfc6266_disposition(fs):
    with tenant_scope(ORG_A):
        key = storage.org_key("exports", "data.csv")
        storage.put_bytes(key, b"col\n1\n")
        url = storage.presigned_get(key, filename="Résumé \"Q3\".csv")
    resp = _get(url.rsplit("/", 1)[1])
    assert resp.status_code == 200 and resp.streaming
    assert b"".join(resp.streaming_content) == b"col\n1\n"
    disposition = resp["Content-Disposition"]
    assert disposition.startswith('attachment; filename="') and "filename*=UTF-8''R%C3%A9sum%C3%A9" in disposition
    assert '"Q3"' not in disposition and "\r" not in disposition
    assert resp["X-Content-Type-Options"] == "nosniff" and "no-store" in resp["Cache-Control"]
    assert resp["Content-Type"].startswith("text/csv")
    assert current_org.get() is None  # the view's tenant scope is closed again


def test_download_view_errors(fs, monkeypatch):
    with tenant_scope(ORG_A):
        key = storage.org_key("gone.csv")
        storage.put_bytes(key, b"x")
        token = storage.sign_download(key, expires_s=60)
        storage.delete_object(key)
    assert _get(token).status_code == 404  # object deleted
    assert _get(token + "tampered").status_code == 404
    now = time.time()
    monkeypatch.setattr(signing.time, "time", lambda: now - 3600)
    with tenant_scope(ORG_A):
        storage.put_bytes(key, b"x")
        old = storage.sign_download(key, expires_s=60)
    monkeypatch.setattr(signing.time, "time", lambda: now)
    resp = _get(old)
    assert resp.status_code == 410 and b"download_expired" in resp.content


def test_download_view_is_disabled_for_s3(fs, settings):
    with tenant_scope(ORG_A):
        key = storage.org_key("a.csv")
        storage.put_bytes(key, b"x")
        token = storage.sign_download(key, expires_s=60)
    settings.STORAGE_BACKEND = "s3"
    assert _get(token).status_code == 404


def test_download_route_is_mounted():
    from django.urls import resolve

    match = resolve("/api/v1/files/abc:def")
    assert match.func is files.download and match.kwargs == {"token": "abc:def"}


# ------------------------------------------------------------------------------------------------ tenant scope
def test_tenant_scope_sets_only_the_contextvar():
    assert current_org.get() is None
    with tenant_scope(str(ORG_A)):
        assert current_org.get() == ORG_A
        with tenant_scope(ORG_A):  # same org nests
            assert current_org.get() == ORG_A
        with pytest.raises(TenantMismatch):
            with tenant_scope(ORG_B):
                pass
    assert current_org.get() is None
    assert storage.org_scope is tenant_scope


# ------------------------------------------------------------------------------------- overwrite while open (W4)
def test_overwrite_and_delete_while_a_download_holds_the_object_open(fs):
    """Windows: a streaming download holds the file open; os.replace onto it used to fail with WinError 5.
    Read handles are share-delete and the replace falls back to a POSIX-semantics rename, so the reader keeps the
    OLD bytes and later reads get the NEW ones (on POSIX this is plain rename semantics)."""
    with tenant_scope(ORG_A):
        key = storage.org_key("exports", "live.csv")
        storage.put_bytes(key, b"old-bytes")
        handle = storage.open_object(key)
        try:
            storage.put_bytes(key, b"new-bytes")
            assert handle.read() == b"old-bytes"
        finally:
            handle.close()
        assert storage.get_object(key) == b"new-bytes"
        handle = storage.open_object(key)
        try:
            storage.delete_object(key)
            assert handle.read() == b"new-bytes"
        finally:
            handle.close()
        assert not storage.object_exists(key)
        assert not [p for p in fs.rglob(".tmp-*")], "no temp files may be left behind"


def test_replace_retries_with_back_off_then_gives_up(monkeypatch, tmp_path):
    calls, sleeps = [], []

    def deny(src, dst):
        calls.append((src, dst))
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(storage.os, "replace", deny)
    monkeypatch.setattr(storage, "_posix_rename_nt", lambda s, d: (_ for _ in ()).throw(PermissionError(5, "x")))
    monkeypatch.setattr(storage.os, "name", "nt")
    with pytest.raises(PermissionError):
        storage._replace_with_retry("a", tmp_path / "b", delays=(0.01, 0.02), sleep=sleeps.append)
    assert len(calls) == 3 and sleeps == [0.01, 0.02]


def test_replace_succeeds_after_a_transient_lock(monkeypatch, tmp_path):
    state = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        state["n"] += 1
        if state["n"] == 1:
            raise PermissionError(32, "in use by another process")
        return real(src, dst)

    src = tmp_path / "src.part"
    src.write_bytes(b"x")
    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage, "_posix_rename_nt", lambda s, d: (_ for _ in ()).throw(PermissionError(32, "x")))
    monkeypatch.setattr(storage.os, "name", "nt")
    sleeps: list[float] = []
    storage._replace_with_retry(str(src), tmp_path / "dst", delays=(0.01,), sleep=sleeps.append)
    assert (tmp_path / "dst").read_bytes() == b"x" and sleeps == [0.01]


def test_replace_does_not_retry_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(storage.os, "name", "posix")
    monkeypatch.setattr(storage.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(PermissionError):
        storage._replace_with_retry("a", tmp_path / "b", sleep=lambda s: pytest.fail("must not sleep"))
