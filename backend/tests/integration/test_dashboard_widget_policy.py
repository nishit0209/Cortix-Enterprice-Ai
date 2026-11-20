"""Dashboard widget data is re-executed under the VIEWER's policy (CONTRACT "W3 additions / Dashboards").

`GET /dashboards/{id}/widgets/{wid}/data` re-runs the saved query through `sqlguard.validate` + `execute` under
the viewer's `effective_policy`, never from stored rows, and audits the refresh (`BiQueryAudit` source
`dashboard`). Dashboards are private to their owner unless `is_shared` (as-built Backend W3: `visible_dashboards`
= own OR shared, anything else 404), so the owner pins the widget on a SHARED board, the only way an analyst can
view it. With ONE widget pinned by the owner from an Ask-AI answer ("total revenue by region"):

  * owner (no policy): the 5 regions + the NULL group, values equal to the independent pandas-free oracle;
  * analyst with a ROW policy region = North: exactly one row, North, with the owner's North value;
  * analyst with a COLUMN deny on revenue: no revenue value at all (guard rejection `column_not_allowed`, or the
    column is absent); never the owner's numbers;
  * analyst with a mask_null on region: the region key is NULL everywhere, the revenue total is preserved;
  * every refresh is audited with source `dashboard`, and the owner's result is unchanged after all of it.

Real Postgres (RLS) + nexora_wh; storage is the in-memory fake; Gemini is the gated fake (never live).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile

from testing.client import api_client_for, tenant_route_exists
from testing.golden import compare, expected_result

REPO = Path(__file__).resolve().parents[3]
SALES = REPO / "testdata" / "generated" / "sales.csv"
REGION_CHECK = {"type": "group", "agg": "sum", "measure": "revenue", "by": "region"}

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        not tenant_route_exists("dashboards"), reason="dashboards not routed yet (Backend W3)"
    ),
]


def _names(columns) -> list[str]:
    return [c["name"] if isinstance(c, dict) else str(c) for c in columns]


def _ingest_sales(client, base: str, capture) -> str:
    up = client.post(
        f"{base}/uploads",
        {"file": SimpleUploadedFile("sales.csv", SALES.read_bytes(), "text/csv")},
        format="multipart",
    )
    assert up.status_code == 201, up.content[:300]
    with capture(execute=True):
        created = client.post(
            f"{base}/datasets",
            {"name": "Sales", "upload_id": up.json()["id"], "classification": "synthetic", "attest": True},
            format="json",
        )
    assert created.status_code == 202, created.content[:300]
    dataset_id = created.json()["dataset"]["id"]
    assert client.get(f"{base}/datasets/{dataset_id}").json()["status"] == "ready"
    return dataset_id


def _pin_region_widget(client, base: str, dataset_id: str) -> tuple[str, str]:
    conv = client.post(f"{base}/bi/conversations", {"dataset_id": dataset_id, "title": "pin"}, format="json")
    assert conv.status_code == 201, conv.content[:300]
    msg = client.post(
        f"{base}/bi/conversations/{conv.json()['id']}/messages",
        {"question": "total revenue by region"},
        format="json",
        HTTP_ACCEPT="application/json",
    )
    assert msg.status_code == 200, msg.content[:300]
    assert msg.json()["sql"]["source"] == "builder"
    board = client.post(f"{base}/dashboards", {"title": "Regional", "is_shared": True}, format="json")
    assert board.status_code == 201, board.content[:300]
    assert board.json().get("is_shared", True) is True, board.json()
    widget = client.post(
        f"{base}/dashboards/{board.json()['id']}/widgets",
        {
            "title": "Revenue by region",
            "kind": "table",
            "message_id": msg.json()["id"],
            "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
        },
        format="json",
    )
    assert widget.status_code == 201, widget.content[:300]
    body = widget.json()
    assert body.get("saved_query_id"), "pinning an answer creates a SavedQuery from the audited executed_sql"
    return board.json()["id"], body["id"]


def _data(client, base: str, board: str, widget: str):
    return client.get(f"{base}/dashboards/{board}/widgets/{widget}/data")


def _rows(resp) -> tuple[list[str], list[tuple]]:
    body = resp.json()
    assert set(body) >= {"columns", "rows", "row_count", "truncated", "refreshed_at"}, sorted(body)
    rows = [tuple(r) for r in body["rows"]]
    assert body["row_count"] == len(rows)
    return _names(body["columns"]), rows


def _policy(client, base: str, dataset_id: str, kind: str, body: dict) -> str:
    resp = client.post(f"{base}/datasets/{dataset_id}/{kind}", body, format="json")
    assert resp.status_code == 201, resp.content[:300]
    return resp.json()["id"]


def _dashboard_audits(org_id) -> int | None:
    model = next(
        (m for m in django_apps.get_app_config("bi").get_models() if m.__name__.endswith("QueryAudit")), None
    )
    if model is None or "source" not in {f.name for f in model._meta.get_fields()}:
        return None
    from apps.core.tenancy import tenant_context
    from testing.db import clear_db_tenant

    try:
        with tenant_context(org_id):
            return model.objects.filter(source="dashboard").count()
    finally:
        clear_db_tenant()


def test_widget_data_follows_the_viewers_policy_exactly(
    two_orgs,
    memory_storage,
    fake_redis,
    fake_embedder,
    gated_fake_gemini,
    network_spy,
    django_capture_on_commit_callbacks,
):
    gated_fake_gemini.respond("*", stream=["Synthetic narration."])
    a = two_orgs.a
    base = f"/api/v1/orgs/{a.slug}"
    owner = api_client_for(a.owner)
    analyst = api_client_for(a.analyst)
    dataset_id = _ingest_sales(owner, base, django_capture_on_commit_callbacks)
    board, widget = _pin_region_widget(owner, base, dataset_id)
    expected = expected_result(REGION_CHECK)

    # owner: the full answer, recomputed by the guard (no stored rows)
    resp = _data(owner, base, board, widget)
    assert resp.status_code == 200, resp.content[:300]
    names, rows = _rows(resp)
    assert compare(expected, names, rows) == [], rows
    audits_before = _dashboard_audits(a.id)

    # analyst without policy sees the same rows
    names_a, rows_a = _rows(_data(analyst, base, board, widget))
    assert sorted(rows_a, key=str) == sorted(rows, key=str)

    # ROW policy: region = North for the analyst only
    row_policy = _policy(
        owner,
        base,
        dataset_id,
        "row-policies",
        {
            "name": "north only",
            "principal_type": "user",
            "principal_id": str(a.analyst.pk),
            "predicate": {"all": [{"col": "region", "op": "=", "value": "North"}]},
        },
    )
    resp = _data(analyst, base, board, widget)
    assert resp.status_code == 200, resp.content[:300]
    names_r, rows_r = _rows(resp)
    assert [r[0] for r in rows_r] == ["North"], rows_r
    assert math.isclose(float(rows_r[0][-1]), float(expected.mapping["North"]), abs_tol=0.01)
    assert owner.delete(f"{base}/datasets/{dataset_id}/row-policies/{row_policy}").status_code == 204

    # COLUMN deny on revenue for the analyst: no revenue values leave the guard
    deny = _policy(
        owner,
        base,
        dataset_id,
        "column-policies",
        {
            "column_name": "revenue",
            "principal_type": "user",
            "principal_id": str(a.analyst.pk),
            "action": "deny",
        },
    )
    resp = _data(analyst, base, board, widget)
    text = resp.content.decode()
    if resp.status_code == 200:
        names_d, rows_d = _rows(resp)
        assert all(n.lower() != "revenue" for n in names_d[1:]) or all(r[-1] is None for r in rows_d), rows_d
    else:
        assert resp.status_code in (400, 403, 422), resp.status_code
        assert "column_not_allowed" in text, text[:300]
    for value in (v for v in expected.mapping.values() if v is not None):
        assert f"{value:.2f}" not in text, "a denied column's value leaked into the widget response"
    assert owner.delete(f"{base}/datasets/{dataset_id}/column-policies/{deny}").status_code == 204

    # COLUMN mask_null on region: one NULL group carrying the full total
    mask = _policy(
        owner,
        base,
        dataset_id,
        "column-policies",
        {
            "column_name": "region",
            "principal_type": "user",
            "principal_id": str(a.analyst.pk),
            "action": "mask_null",
        },
    )
    resp = _data(analyst, base, board, widget)
    assert resp.status_code == 200, resp.content[:300]
    names_m, rows_m = _rows(resp)
    assert {r[0] for r in rows_m} == {None}, rows_m
    total = sum(float(v) for v in expected.mapping.values() if v is not None)
    assert math.isclose(sum(float(r[-1]) for r in rows_m), total, abs_tol=0.05)
    for region in ("North", "South", "East", "West", "Central"):
        assert f'"{region}"' not in resp.content.decode()
    assert owner.delete(f"{base}/datasets/{dataset_id}/column-policies/{mask}").status_code == 204

    # the owner's view never changed, every refresh was audited as source=dashboard
    names_o, rows_o = _rows(_data(owner, base, board, widget))
    assert compare(expected, names_o, rows_o) == []
    audits_after = _dashboard_audits(a.id)
    if audits_before is not None:
        assert audits_after >= audits_before + 5, (audits_before, audits_after)
    assert network_spy.external == []
    assert gated_fake_gemini.blocked == []


def test_widget_data_for_a_non_member_or_other_org_is_404(
    two_orgs,
    memory_storage,
    fake_redis,
    fake_embedder,
    gated_fake_gemini,
    network_spy,
    django_capture_on_commit_callbacks,
):
    gated_fake_gemini.respond("*", stream=["Synthetic narration."])
    a, b = two_orgs
    base = f"/api/v1/orgs/{a.slug}"
    owner = api_client_for(a.owner)
    dataset_id = _ingest_sales(owner, base, django_capture_on_commit_callbacks)
    board, widget = _pin_region_widget(owner, base, dataset_id)
    for client, path in (
        (api_client_for(b.owner), f"/api/v1/orgs/{b.slug}/dashboards/{board}/widgets/{widget}/data"),
        (api_client_for(b.owner), f"{base}/dashboards/{board}/widgets/{widget}/data"),
        (api_client_for(two_orgs.outsider), f"{base}/dashboards/{board}/widgets/{widget}/data"),
    ):
        resp = client.get(path)
        assert resp.status_code == 404, (path, resp.status_code)
        assert b"North" not in resp.content
