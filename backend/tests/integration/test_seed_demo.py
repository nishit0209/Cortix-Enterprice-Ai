"""`manage.py seed_demo` (CONTRACT "Local runtime: Demo data", Backend `apps.platform_admin`).

After `seed_demo` the demo login must show REAL database content on every screen:
  * the synthetic CSVs from testdata/generated/ (sales, customers, products, orders, support tickets) are datasets
    in status ready, classified synthetic, with the exact CSV row counts, column profiles and a quality score;
  * `/data-stats` KPIs are non-zero;
  * the default dashboard has 4-6 widgets and every data widget returns rows; the revenue-by-region and the total
    revenue KPI equal the independent oracle computed from sales.csv;
  * a BI builder question ("total revenue by region") answers correctly through the public API;
  * the org keeps the default AI data policy `synthetic_only`;
  * running the command twice creates no duplicates (users, orgs, datasets, versions, dashboards, widgets,
    conversations) and the second run leaves every row count unchanged.

Each seeded file is matched to EXACTLY one dataset (exact normalised name or the file named in the description,
`testing.flows.find_seeded_dataset`): seed_demo also ingests customers_crm.csv, products_supplier_b.csv ... whose
names contain the shorter stems. The second run re-sets `--password` (a new salted hash), which invalidates
existing sessions by design (Django session auth hash), so the client logs in again before the idempotency check.

Transactional DB test (the command runs in autocommit like in production). Gemini is the gated fake (never
live), outbound network is blocked, storage is the filesystem backend in a temp dir.
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path

import pytest
from django.core.management import call_command, get_commands

from testing.client import api_client_for
from testing.flows import find_seeded_dataset, items
from testing.golden import compare, expected_result

REPO = Path(__file__).resolve().parents[3]
GEN = REPO / "testdata" / "generated"
SEED_FILES = ("sales.csv", "customers.csv", "products.csv", "orders.csv", "support_tickets.csv")
DEMO_EMAIL = "demo-qa@example.test"
DEMO_PASSWORD = "Synthetic-Demo-Passw0rd!"
REGION_CHECK = {"type": "group", "agg": "sum", "measure": "revenue", "by": "region"}
TOTAL_CHECK = {"type": "total", "agg": "sum", "measure": "revenue"}

pytestmark = [
    pytest.mark.skipif(
        "seed_demo" not in get_commands(), reason="manage.py seed_demo not implemented (Backend W3)"
    ),
    pytest.mark.django_db(transaction=True, databases="__all__"),
]


def _csv_rows(name: str) -> int:
    with (GEN / name).open(encoding="utf-8", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def _org_option() -> str:
    from django.core.management import load_command_class

    cmd = load_command_class(get_commands()["seed_demo"], "seed_demo")
    parser = cmd.create_parser("manage.py", "seed_demo")
    options = {s for a in parser._actions for s in a.option_strings}
    for opt in ("--org-name", "--org"):
        if opt in options:
            return opt
    pytest.fail(f"seed_demo has neither --org-name nor --org (options: {sorted(options)})")


def _seed() -> str:
    out = io.StringIO()
    call_command(
        "seed_demo",
        "--email",
        DEMO_EMAIL,
        "--password",
        DEMO_PASSWORD,
        _org_option(),
        "QA Demo Org",
        stdout=out,
    )
    return out.getvalue()


@pytest.fixture
def runtime(settings, tmp_path, fake_embedder, gated_fake_gemini, network_spy):
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = tmp_path / "storage"
    settings.JOB_EVENTS_BACKEND = "db"
    gated_fake_gemini.respond("*", stream=["Synthetic narration."])
    return gated_fake_gemini


def _ok(resp):
    assert resp.status_code == 200, (resp.status_code, resp.content[:300])
    return resp


def _snapshot(client, base: str) -> dict:
    datasets = items(_ok(client.get(f"{base}/datasets")))
    boards = items(_ok(client.get(f"{base}/dashboards")))
    widgets = sum(len(_ok(client.get(f"{base}/dashboards/{b['id']}")).json()["widgets"]) for b in boards)
    conversations = items(_ok(client.get(f"{base}/bi/conversations")))
    versions = {}
    for ds in datasets:
        resp = client.get(f"{base}/datasets/{ds['id']}/versions")
        versions[ds["id"]] = len(items(resp)) if resp.status_code == 200 else None
    return {
        "datasets": sorted((d["name"], d["row_count"]) for d in datasets),
        "versions": versions,
        "dashboards": len(boards),
        "widgets": widgets,
        "conversations": len(conversations),
    }


def test_seed_demo_produces_a_real_working_org_and_is_idempotent(runtime, network_spy):
    from apps.accounts.models import User

    output = _seed()
    assert DEMO_EMAIL in output, "the command must print the login"

    user = User.objects.get(email=DEMO_EMAIL)
    client = api_client_for(user)
    me = client.get("/api/v1/me").json()
    orgs = me["orgs"]
    assert len(orgs) == 1, orgs
    slug = orgs[0]["slug"]
    assert slug in output, "the command must print the org slug"
    base = f"/api/v1/orgs/{slug}"
    detail = client.get(base)
    org = detail.json() if detail.status_code == 200 else orgs[0]
    assert org.get("ai_data_policy", "synthetic_only") == "synthetic_only", (
        "the demo org keeps the default policy"
    )

    # ---------------------------------------------------------------- datasets: ready, profiled, scored
    datasets = items(_ok(client.get(f"{base}/datasets")))
    seeded = {}
    for name in SEED_FILES:
        ds = find_seeded_dataset(datasets, name)
        seeded[name] = ds
        assert ds["status"] == "ready", (name, ds.get("last_error"))
        assert ds["classification"] == "synthetic", name
        assert ds["row_count"] == _csv_rows(name), (name, ds["row_count"])
        assert ds["quality_score"] is not None and 0 < ds["quality_score"] <= 100, (name, ds["quality_score"])
        columns = client.get(f"{base}/datasets/{ds['id']}/columns").json()
        assert columns and all(c.get("stats") for c in columns), f"{name}: columns not profiled"
        quality = client.get(f"{base}/datasets/{ds['id']}/quality")
        assert quality.status_code == 200 and quality.json().get("score") is not None, name

    # ---------------------------------------------------------------- data-stats KPIs
    stats = client.get(f"{base}/data-stats").json()
    assert stats["datasets"] >= len(SEED_FILES)
    assert stats["rows_total"] >= sum(_csv_rows(n) for n in SEED_FILES)
    assert stats["avg_quality"] and stats["avg_quality"] > 0
    assert stats["storage_bytes"] > 0

    # ---------------------------------------------------------------- default dashboard
    boards = items(client.get(f"{base}/dashboards"))
    defaults = [b for b in boards if b.get("is_default")]
    assert len(defaults) == 1, boards
    board = client.get(f"{base}/dashboards/{defaults[0]['id']}").json()
    widgets = board["widgets"]
    assert 4 <= len(widgets) <= 6, len(widgets)
    total = expected_result(TOTAL_CHECK).value
    regions = expected_result(REGION_CHECK)
    saw_regions = saw_total = False
    for w in widgets:
        if w["kind"] == "text":
            continue
        resp = client.get(f"{base}/dashboards/{board['id']}/widgets/{w['id']}/data")
        assert resp.status_code == 200, (w["title"], resp.content[:300])
        body = resp.json()
        assert body["row_count"] > 0 and body["rows"], f"widget {w['title']!r} returned no rows"
        assert body.get("refreshed_at"), "widget data must be re-executed (refreshed_at)"
        names = [c["name"] if isinstance(c, dict) else str(c) for c in body["columns"]]
        rows = [tuple(r) for r in body["rows"]]
        if w["kind"] == "kpi" and len(rows) == 1 and math.isclose(float(rows[0][-1]), total, abs_tol=0.05):
            saw_total = True
        if len(names) >= 2 and {r[0] for r in rows} >= {"North", "South", "East", "West", "Central"}:
            assert compare(regions, names, rows) == [], rows
            saw_regions = True
    assert saw_total, "expected a KPI widget with the total revenue of sales.csv"
    assert saw_regions, "expected a revenue-by-region widget"

    # ---------------------------------------------------------------- a BI builder question answers
    sales_id = seeded["sales.csv"]["id"]
    conv = client.post(f"{base}/bi/conversations", {"dataset_id": sales_id, "title": "QA"}, format="json")
    assert conv.status_code == 201, conv.content[:300]
    msg = client.post(
        f"{base}/bi/conversations/{conv.json()['id']}/messages",
        {"question": "total revenue by region"},
        format="json",
        HTTP_ACCEPT="application/json",
    )
    assert msg.status_code == 200, msg.content[:300]
    body = msg.json()
    assert body["sql"]["source"] == "builder"
    names = [c["name"] if isinstance(c, dict) else str(c) for c in body["result"]["columns"]]
    assert compare(regions, names, [tuple(r) for r in body["result"]["rows"]]) == []
    client.delete(f"{base}/bi/conversations/{conv.json()['id']}")
    assert "nl2sql.generate" not in [c.prompt_key for c in runtime.calls]
    assert runtime.blocked == [] and network_spy.external == []

    # ---------------------------------------------------------------- idempotency
    before = _snapshot(client, base)
    assert before["conversations"] >= 1, "seed_demo creates one sample conversation"
    _seed()
    assert User.objects.filter(email=DEMO_EMAIL).count() == 1
    user = User.objects.get(email=DEMO_EMAIL)
    assert user.check_password(DEMO_PASSWORD), "--password must still be the demo login after a re-run"
    # The re-run re-hashed the password (new salt): the old session is invalid by design, so log in again.
    client = api_client_for(user)
    after = _snapshot(client, base)
    assert after == before, f"second run changed the demo org:\nbefore={before}\nafter={after}"
    assert len(_ok(client.get("/api/v1/me")).json()["orgs"]) == 1
