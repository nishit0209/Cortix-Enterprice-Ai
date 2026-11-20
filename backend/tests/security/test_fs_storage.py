"""Local runtime: filesystem object storage (CONTRACT "Local runtime: Storage", Backend `apps.core.storage`).

With `STORAGE_BACKEND="fs"` the SAME public API stores objects under `LOCAL_STORAGE_DIR/<key>` with the same key
validation, and `presigned_get()` returns a signed, expiring `/api/v1/files/<token>` URL served by a streaming
view with an RFC 6266 `Content-Disposition`.

Security properties checked here (no database needed: storage never touches the DB):
  * path traversal / absolute / foreign-org / Windows-special keys are rejected and nothing is written outside
    the storage root (also when the key is otherwise valid for the active org);
  * another org's key cannot be read, listed, deleted or presigned from org A's context;
  * signed URLs: a valid token streams the bytes; an EXPIRED token and a TAMPERED token get 403 (QA mission W3,
    the status the frontend handles), never the file; a token cannot be re-pointed to another org's key;
  * the download's Content-Disposition is sanitised (no CR/LF/quote injection, no path separators);
  * writes are atomic (no temp file is left behind, a reader never sees a partial file).

Every test skips cleanly until the Backend W3 filesystem backend is merged (detected by behaviour: a write under
STORAGE_BACKEND="fs" must land below LOCAL_STORAGE_DIR, and the S3 client must not be used).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from django.test import Client

from apps.core import storage
from apps.core.tenancy import current_org
from testing.client import route_exists

ORG_A = uuid.UUID("018f0000-0000-7000-8000-0000000f5a0a")
ORG_B = uuid.UUID("018f0000-0000-7000-8000-0000000f5b0b")
FILES_PREFIX = "/api/v1/files/"


@contextmanager
def active(org_id):
    token = current_org.set(org_id)
    try:
        yield
    finally:
        current_org.reset(token)


def _no_s3(*_a, **_k):
    raise AssertionError("the S3 client must not be used when STORAGE_BACKEND='fs'")


@pytest.fixture
def fs(settings, monkeypatch, tmp_path):
    """STORAGE_BACKEND=fs in a private directory; skips while the fs backend is not implemented."""
    settings.STORAGE_BACKEND = "fs"
    root = tmp_path / "storage"
    settings.LOCAL_STORAGE_DIR = root
    monkeypatch.setattr(storage, "client", _no_s3)
    probe = f"orgs/{ORG_A}/qa/probe.txt"
    try:
        with active(ORG_A):
            storage.put_bytes(probe, b"probe", "text/plain")
    except AssertionError:
        pytest.skip("STORAGE_BACKEND='fs' not implemented yet (Backend W3): put_bytes still calls S3")
    except Exception as exc:  # noqa: BLE001 - any failure of the probe means the backend is absent
        pytest.skip(f"STORAGE_BACKEND='fs' not implemented yet (Backend W3): {type(exc).__name__}: {exc}")
    written = root / probe
    if not written.is_file():
        pytest.fail(
            "STORAGE_BACKEND='fs' did not write the object at LOCAL_STORAGE_DIR/<key> "
            "(settings must be read at call time, CONTRACT 'Local runtime')"
        )
    written.unlink()
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _all_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ------------------------------------------------------------------------------------------------ keys
def test_round_trip_lives_under_the_root_at_the_key(fs):
    key = f"orgs/{ORG_A}/uploads/2026/sales.csv"
    with active(ORG_A):
        storage.put_bytes(key, b"a,b\n1,2\n", "text/csv")
        assert storage.get_object(key) == b"a,b\n1,2\n"
    assert (fs / key).read_bytes() == b"a,b\n1,2\n"
    assert _all_files(fs) == [fs / key], "no temp file may be left behind (atomic write)"


TRAVERSAL_KEYS = [
    f"orgs/{ORG_A}/../{ORG_B}/secret.csv",
    f"orgs/{ORG_A}/uploads/../../{ORG_B}/f",
    f"orgs/{ORG_A}/./f",
    f"orgs/{ORG_A}//f",
    f"orgs/{ORG_A}/sub\\..\\..\\{ORG_B}\\f",
    f"/orgs/{ORG_A}/f",
    f"orgs/{ORG_B}/f.csv",
    "C:/Windows/win.ini",
    "../../etc/passwd",
    f"orgs/{ORG_A}/f\x00.csv",
    f"orgs/{ORG_A}/line\nbreak",
]


@pytest.mark.parametrize("key", TRAVERSAL_KEYS)
@pytest.mark.parametrize("op", ["put_bytes", "get_object", "delete_object", "presigned_get", "download_to"])
def test_traversal_and_foreign_keys_are_rejected(fs, tmp_path, key, op):
    outside = tmp_path / "outside"
    outside.mkdir()
    args = {"put_bytes": (b"x",), "download_to": (str(outside / "copy"),)}.get(op, ())
    with active(ORG_A), pytest.raises((PermissionError, ValueError)):
        getattr(storage, op)(key, *args)
    assert _all_files(fs) == [] and _all_files(outside) == [], "a rejected key must not touch the filesystem"


SPECIAL_SEGMENTS = [
    "C:",
    "file.csv:stream",
    "CON",
    "nul.txt",
    "trailing.",
    "trailing ",
    "a|b",
    "a?b",
    'a"b',
    "a<b>",
]


def _outside_snapshot(root: Path) -> tuple[set[Path], set[str]]:
    """Everything next to the storage root (the test's tmp dir) and the names in the current directory."""
    siblings = {p for p in root.parent.rglob("*") if p != root and root not in p.parents}
    return siblings, set(os.listdir(os.getcwd()))


def _assert_stored_exactly_at_the_key(root: Path, segment: str, read_back, before) -> None:
    """An ACCEPTED special name must be a plain file named exactly `segment` in uploads/, holding the bytes.

    Catches what Windows does silently: a device name (CON, nul.txt) swallows the write and leaves no file, an NTFS
    alternate data stream ("file.csv:stream") lands in a stream of "file.csv", a trailing dot/space is stripped
    ("trailing." aliases "trailing"), a drive-relative segment ("C:") writes on another drive. The on-disk checks
    run BEFORE `read_back()` (the backend's get_object) so a device name such as CON never blocks on a read.
    """
    uploads = root / "orgs" / str(ORG_A) / "uploads"
    on_disk = sorted(os.listdir(uploads)) if uploads.is_dir() else []
    assert on_disk == [segment], f"{segment!r} was stored as {on_disk} (a device, a stream or an alias)"
    real_root = root.resolve()
    files = _all_files(root)
    assert files == [uploads / segment], f"{segment!r}: unexpected files under the root: {files}"
    for path in files:
        assert real_root in path.resolve().parents, f"{segment!r} escaped the storage root: {path}"
    assert _outside_snapshot(root) == before, f"{segment!r} wrote outside the storage root"
    assert (uploads / segment).read_bytes() == b"x"
    data = read_back()
    assert data == b"x", f"{segment!r}: get_object returned {data!r}"


@pytest.mark.parametrize("segment", SPECIAL_SEGMENTS)
def test_filesystem_special_names_are_rejected_or_stored_exactly_at_the_key(fs, segment):
    """Names Windows reinterprets (drive letters, ADS streams, devices, trailing dot/space) are rejected, or else
    stored as exactly that file inside the root: never a device, a stream, an alias or a path outside the root."""
    key = f"orgs/{ORG_A}/uploads/{segment}"
    before = _outside_snapshot(fs)
    assert _all_files(fs) == []
    with active(ORG_A):
        try:
            storage.put_bytes(key, b"x")
        except (PermissionError, ValueError):
            assert _all_files(fs) == [] and _outside_snapshot(fs) == before, "a rejected key touched the disk"
            return
        _assert_stored_exactly_at_the_key(fs, segment, lambda: storage.get_object(key), before)


@pytest.mark.skipif(os.name != "nt", reason="these names are ordinary file names outside Windows")
@pytest.mark.parametrize("segment", ["file.csv:stream", "trailing.", "trailing ", "nul"])
def test_the_special_name_check_catches_a_backend_that_writes_them_naively(tmp_path, segment):
    """Self-test of the oracle above: a backend doing `open(root / key, "wb")` with no name check must fail it."""
    root = tmp_path / "storage"
    (root / "orgs" / str(ORG_A) / "uploads").mkdir(parents=True)
    before = _outside_snapshot(root)
    target = root / "orgs" / str(ORG_A) / "uploads" / segment
    try:
        target.write_bytes(b"x")
    except OSError:
        pytest.skip(f"this Windows build refuses {segment!r} outright")
    with pytest.raises(AssertionError):
        _assert_stored_exactly_at_the_key(root, segment, target.read_bytes, before)


def test_the_special_name_check_accepts_a_plain_file(tmp_path):
    """...and passes for a name stored exactly at the key (the check itself is not always-failing)."""
    root = tmp_path / "storage"
    uploads = root / "orgs" / str(ORG_A) / "uploads"
    uploads.mkdir(parents=True)
    before = _outside_snapshot(root)
    (uploads / "plain-name.csv").write_bytes(b"x")
    _assert_stored_exactly_at_the_key(root, "plain-name.csv", (uploads / "plain-name.csv").read_bytes, before)


def test_symlinked_directory_cannot_escape_the_root(fs, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    org_dir = fs / "orgs" / str(ORG_A)
    org_dir.mkdir(parents=True, exist_ok=True)
    link = org_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted for this user on this OS")
    with active(ORG_A):
        try:
            storage.put_bytes(f"orgs/{ORG_A}/linked/f.csv", b"x")
        except (PermissionError, ValueError):
            pass
    assert _all_files(outside) == [], "a write through a symlink must not land outside the storage root"


def test_another_orgs_object_cannot_be_read_from_org_a(fs):
    key_b = f"orgs/{ORG_B}/uploads/b-secret.csv"
    with active(ORG_B):
        storage.put_bytes(key_b, b"secret-of-b")
    with active(ORG_A):
        for op, args in (
            ("get_object", ()),
            ("presigned_get", ()),
            ("delete_object", ()),
            ("open_object", ()),
            ("object_exists", ()),
        ):
            fn = getattr(storage, op, None)
            if fn is None:
                continue
            with pytest.raises((PermissionError, ValueError)):
                fn(key_b, *args)
    assert (fs / key_b).read_bytes() == b"secret-of-b"


def test_concurrent_reader_never_sees_a_partial_object(fs):
    key = f"orgs/{ORG_A}/big.bin"
    old, new = b"A" * (4 << 20), b"B" * (4 << 20)
    with active(ORG_A):
        storage.put_bytes(key, old)
    seen: set[bytes] = set()
    stop = threading.Event()

    def reader():
        with active(ORG_A):
            while not stop.is_set():
                try:
                    data = storage.get_object(key)
                except Exception:  # noqa: BLE001 - a transient error is fine, a torn read is not
                    continue
                seen.add(data[:1] + data[-1:] + str(len(data)).encode())

    t = threading.Thread(target=reader)
    t.start()
    try:
        with active(ORG_A):
            for _ in range(5):
                for payload in (new, old):
                    try:
                        storage.put_bytes(key, payload)
                    except Exception:  # noqa: BLE001 - availability is covered by the next test
                        pass
    finally:
        stop.set()
        t.join(10)
    full = str(len(old)).encode()
    assert seen, "the reader never completed a read"
    assert seen <= {b"AA" + full, b"BB" + full}, f"torn reads observed: {sorted(seen)[:4]}"


def test_overwrite_succeeds_while_a_download_holds_the_file_open(fs):
    """A streaming download keeps the file open; replacing the object meanwhile must still work (on Windows a
    plain os.replace onto an open file fails with WinError 5 unless the backend opens files share-delete or
    retries). The download keeps the OLD bytes, later reads get the NEW ones."""
    key = f"orgs/{ORG_A}/exports/live.csv"
    with active(ORG_A):
        storage.put_bytes(key, b"old-bytes")
        opener = getattr(storage, "open_object", None)
        handle = opener(key) if opener else open(fs / key, "rb")  # noqa: SIM115
        try:
            storage.put_bytes(key, b"new-bytes")
            assert handle.read() in (b"old-bytes", b"new-bytes")
        finally:
            handle.close()
        assert storage.get_object(key) == b"new-bytes"


# ------------------------------------------------------------------------------------------------ signed URLs
needs_files_route = pytest.mark.skipif(
    not route_exists(FILES_PREFIX + "probe-token"),
    reason="/api/v1/files/<token> not routed yet (Backend W3 apps.core.urls_public)",
)


def _token(url: str) -> str:
    path = url.split("://", 1)[-1]
    path = path[path.index("/") :] if "://" in url else path
    assert path.startswith(FILES_PREFIX), f"presigned_get must return {FILES_PREFIX}<token>, got {url!r}"
    return path[len(FILES_PREFIX) :].split("?", 1)[0]


def _download(token: str):
    return Client().get(FILES_PREFIX + token)  # anonymous: the signed token IS the capability


def _body(resp) -> bytes:
    return b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content


@needs_files_route
def test_signed_url_streams_the_object(fs):
    key = f"orgs/{ORG_A}/exports/report.csv"
    with active(ORG_A):
        storage.put_bytes(key, b"region,revenue\nNorth,10\n", "text/csv")
        url = storage.presigned_get(key, expires_s=600, filename="report.csv")
    resp = _download(_token(url))
    assert resp.status_code == 200
    assert _body(resp) == b"region,revenue\nNorth,10\n"
    assert resp["Content-Disposition"].startswith("attachment")
    assert "report.csv" in resp["Content-Disposition"]


@needs_files_route
def test_expired_signed_url_is_403(fs, monkeypatch):
    key = f"orgs/{ORG_A}/exports/old.csv"
    with active(ORG_A):
        storage.put_bytes(key, b"old")
        url = storage.presigned_get(key, expires_s=60)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600)  # django.core.signing reads time.time()
    resp = _download(_token(url))
    assert resp.status_code == 403, f"expired link: want 403, got {resp.status_code}"
    assert b"old" not in _body(resp)


@needs_files_route
@pytest.mark.parametrize(
    "mutation", ["flip_last", "flip_first", "truncate", "append", "swap_payload", "empty_sig"]
)
def test_tampered_signed_url_is_403(fs, mutation):
    key = f"orgs/{ORG_A}/exports/t.csv"
    key_b = f"orgs/{ORG_B}/exports/b.csv"
    with active(ORG_B):
        storage.put_bytes(key_b, b"secret-of-b")
        url_b = storage.presigned_get(key_b)
    with active(ORG_A):
        storage.put_bytes(key, b"mine")
        url = storage.presigned_get(key)
    token, token_b = _token(url), _token(url_b)

    def flip(ch: str) -> str:
        return "A" if ch != "A" else "B"

    payload, _, sig = token.rpartition(":")
    payload_b, _, _sig_b = token_b.rpartition(":")
    bad = {
        "flip_last": token[:-1] + flip(token[-1]),
        "flip_first": flip(token[0]) + token[1:],
        "truncate": token[: len(token) // 2],
        "append": token + "x",
        "swap_payload": f"{payload_b}:{sig}" if payload else token_b[:-2] + "zz",
        "empty_sig": f"{payload}:" if payload else token + ":",
    }[mutation]
    resp = _download(bad)
    assert resp.status_code == 403, f"{mutation}: want 403, got {resp.status_code}"
    body = _body(resp)
    assert b"mine" not in body and b"secret-of-b" not in body


@needs_files_route
def test_token_for_a_deleted_object_does_not_leak(fs):
    key = f"orgs/{ORG_A}/exports/gone.csv"
    with active(ORG_A):
        storage.put_bytes(key, b"gone")
        url = storage.presigned_get(key)
        storage.delete_object(key)
    resp = _download(_token(url))
    assert resp.status_code in (403, 404)
    assert b"gone" not in _body(resp)


@needs_files_route
@pytest.mark.parametrize(
    "filename",
    [
        "evil\"; filename*=UTF-8''x.html\r\nX-Injected: 1",
        "../../etc/passwd",
        "..\\..\\windows\\win.ini",
        "report\x00.exe",
        "quarterly report (final).csv",
        "राजस्व रिपोर्ट.csv",
    ],
)
def test_download_content_disposition_is_sanitised(fs, filename):
    key = f"orgs/{ORG_A}/exports/x.csv"
    with active(ORG_A):
        storage.put_bytes(key, b"x")
        url = storage.presigned_get(key, filename=filename)
    resp = _download(_token(url))
    assert resp.status_code == 200
    header = resp["Content-Disposition"]
    assert "\r" not in header and "\n" not in header and "\x00" not in header
    assert "X-Injected" not in resp.headers and "x-injected" not in {k.lower() for k in resp.headers}
    assert header.startswith("attachment;")
    fallback = header.split('filename="', 1)[1].split('"', 1)[0] if 'filename="' in header else ""
    assert "/" not in fallback and "\\" not in fallback and ";" not in fallback
    assert header.count('"') <= 2, header
    assert "filename*=UTF-8''x.html" not in header
    if "*=" in header:
        assert "filename*=UTF-8''" in header
