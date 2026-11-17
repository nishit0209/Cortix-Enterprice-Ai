"""Shared fixtures/helpers for the data-engineering apps' tests (imported by each app's tests/conftest.py).

All data is synthetic. Object storage is replaced by an in-memory fake that still enforces the
orgs/<org_id>/ key prefix; Postgres (nexora_app + nexora_wh) is real.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterable

import pytest
from django.conf import settings

from apps.core import storage


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, key, fileobj, content_type="application/octet-stream"):
        storage._check_key(key)
        self.objects[key] = fileobj.read()

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        storage._check_key(key)
        self.objects[key] = bytes(data)

    def get_object(self, key):
        storage._check_key(key)
        return self.objects[key]

    def download_to(self, key, path):
        storage._check_key(key)
        with open(path, "wb") as fh:
            fh.write(self.objects[key])

    def delete_object(self, key):
        storage._check_key(key)
        self.objects.pop(key, None)


@pytest.fixture
def fake_storage(monkeypatch):
    fake = FakeStorage()
    for name in ("put_object", "put_bytes", "get_object", "download_to", "delete_object"):
        monkeypatch.setattr(storage, name, getattr(fake, name))
    return fake


def _drop_test_warehouse_objects() -> None:
    """Drop every schema/role created with this session's test prefix (never touches 'org_' objects)."""
    import psycopg

    from apps.warehouse import naming

    prefix = settings.WH_SCHEMA_PREFIX
    if prefix == "org_" or not prefix.startswith("t_"):
        return
    role_prefix = naming.reader_role_prefix()
    try:
        with psycopg.connect(settings.WH_DATABASE_URL_LOADER, autocommit=True, connect_timeout=3) as conn:
            schemas = [r[0] for r in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE left(nspname, %s) = %s", [len(prefix), prefix]).fetchall()]
            for s in schemas:
                conn.execute(f'DROP SCHEMA IF EXISTS "{naming.check_ident(s)}" CASCADE')
            roles = [r[0] for r in conn.execute(
                "SELECT rolname FROM pg_roles WHERE left(rolname, %s) = %s", [len(role_prefix), role_prefix]).fetchall()]
            for r in roles:
                conn.execute(f'DROP ROLE IF EXISTS "{naming.check_ident(r)}"')
    except psycopg.Error:
        pass


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_warehouse():
    yield
    _drop_test_warehouse_objects()


@pytest.fixture
def make_org(db):
    from apps.accounts.models import User
    from apps.orgs.services import create_organization

    counter = {"n": 0}

    def _make(name: str = "Acme Analytics", role: str = "owner"):
        counter["n"] += 1
        owner = User.objects.create_user(f"owner{counter['n']}-{name.split()[0].lower()}@example.test", "pw-Owner-123456")
        org = create_organization(name=f"{name} {counter['n']}", owner=owner)
        return owner, org

    return _make


@pytest.fixture
def two_orgs(make_org):
    alice, org_a = make_org("Acme Analytics")
    bob, org_b = make_org("Beta Retail")
    return alice, org_a, bob, org_b


def add_member(org, email: str, role_key: str):
    from apps.accounts.models import User
    from apps.core.tenancy import tenant_context
    from apps.orgs.models import Membership, Role

    user = User.objects.create_user(email, "pw-Member-123456")
    with tenant_context(org.id):
        Membership.objects.create(org_id=org.id, user=user, role=Role.objects.get(key=role_key))
    return user


def csv_bytes(rows: Iterable[Iterable], *, delimiter: str = ",", encoding: str = "utf-8") -> bytes:
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode(encoding)


def json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def xlsx_bytes(rows: list[list], *, sheet: str = "Data", empty_first_sheet: bool = False, merge: str | None = None) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    if empty_first_sheet:
        ws.title = "Cover"
        ws = wb.create_sheet(sheet)
    else:
        ws.title = sheet
    for row in rows:
        ws.append(row)
    if merge:
        ws.merge_cells(merge)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SAMPLE_CUSTOMERS = [
    ["Customer ID", "Full Name", "Email", "City", "Signup Date", "Lifetime Value (₹)", "Active"],
    *[
        [
            1000 + i,
            f"Test Person {i}",
            f"person{i}@example.test",
            ["Mumbai", "Pune", "Delhi", "Chennai"][i % 4],
            f"2024-01-{(i % 28) + 1:02d}",
            f"₹{(i * 1234) % 100000:,}.50",
            "yes" if i % 3 else "no",
        ]
        for i in range(40)
    ],
]


def upload_file(client, org, filename: str, content: bytes):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return client.post(
        f"/api/v1/orgs/{org.slug}/uploads",
        {"file": SimpleUploadedFile(filename, content)},
        format="multipart",
    )


def ingest(client, org, filename: str, content: bytes, capture, *, name: str = "Customers", **extra):
    """Upload + create a dataset and run the (eager) ingest task. Returns (create_response, dataset_json)."""
    up = upload_file(client, org, filename, content)
    assert up.status_code == 201, up.content
    with capture(execute=True):
        resp = client.post(
            f"/api/v1/orgs/{org.slug}/datasets", {"name": name, "upload_id": up.json()["id"], **extra}, format="json"
        )
    assert resp.status_code == 202, resp.content
    detail = client.get(f"/api/v1/orgs/{org.slug}/datasets/{resp.json()['dataset']['id']}")
    return resp, detail.json()
