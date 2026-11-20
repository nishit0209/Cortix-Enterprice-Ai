"""Dashboards + widgets API (CONTRACT "W3 additions / Dashboards") end to end, as nexora_app under RLS.

Governance is REAL (dataset ACLs, column and row policies); the semantic layer, SQL guard / executor and NL->SQL are
the in-memory fakes of apps/bi/tests/fakes.py, so every widget-data refresh is visible as one validate + execute call
with the policy it ran under. No Gemini, no warehouse."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection

from apps.bi.dashboards import MAX_WIDGETS
from apps.bi.models import BiQueryAudit, Dashboard, DashboardWidget, SavedQuery
from apps.bi.tests.fakes import DATASET_ID, install
from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, member_of, org_url
from apps.governance.models import AccessHistory, ColumnPolicy, DatasetAcl, RowPolicy
from apps.orgs.models import Membership, Role
from nexora.contracts import ColumnMeta, ResultSet, SqlProposal

pytestmark = pytest.mark.django_db

GOOD_SQL = "SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region"
TOTAL_SQL = "SELECT SUM(amount) AS revenue FROM sales"
BAR = {"type": "bar", "x": "region", "y": ["revenue"]}
SUMMARY_KEYS = {"id", "title", "description", "is_default", "widget_count", "updated_at"}
WIDGET_KEYS = {"id", "title", "kind", "saved_query_id", "chart", "layout", "text"}
DATA_KEYS = {"columns", "rows", "row_count", "truncated", "chart", "refreshed_at"}


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch)


# ------------------------------------------------------------------------------------------------ helpers
def durl(org, *parts) -> str:
    return org_url(org, "/".join(["dashboards", *map(str, parts)]))


def new_dashboard(client, org, **payload) -> dict:
    r = client.post(durl(org), {"title": "Revenue", **payload}, format="json")
    assert r.status_code == 201, r.content
    return r.json()


def new_saved_query(client, org, *, name="Revenue by region", sql=GOOD_SQL, chart=BAR, shared=False) -> dict:
    payload = {"name": name, "dataset_id": str(DATASET_ID), "sql": sql}
    if chart is not None:
        payload["chart"] = chart
    r = client.post(org_url(org, "saved-queries"), payload, format="json")
    assert r.status_code == 201, r.content
    sq = r.json()
    if shared:
        r = client.patch(org_url(org, f"saved-queries/{sq['id']}"), {"is_shared": True}, format="json")
        assert r.status_code == 200, r.content
    return sq


def add_widget(client, org, dash_id, **payload):
    return client.post(durl(org, dash_id, "widgets"), payload, format="json")


def new_widget(client, org, dash_id, **payload) -> dict:
    r = add_widget(client, org, dash_id, **payload)
    assert r.status_code == 201, r.content
    return r.json()


def data_url(org, dash_id, widget_id) -> str:
    return durl(org, dash_id, "widgets", widget_id, "data")


def shared_dashboard_with_widget(client, org) -> tuple[dict, dict, dict]:
    sq = new_saved_query(client, org)
    dash = new_dashboard(client, org, title="Shared revenue", is_shared=True)
    widget = new_widget(client, org, dash["id"], title="Revenue by region", kind="chart", saved_query_id=sq["id"],
                        layout={"x": 0, "y": 0, "w": 6, "h": 4})
    return dash, widget, sq


def other_org_owner(other_org):
    with tenant_context(other_org.id):
        return Membership.objects.get().user


def executions(fake) -> int:
    return len([c for c in fake.calls if c[0] == "execute"])


# ================================================================================================ dashboards
def test_dashboard_crud_shapes_defaults_and_audit(org, owner, owner_client, fake):
    first = new_dashboard(owner_client, org, title="Revenue", description="Q3 numbers")
    assert SUMMARY_KEYS | {"widgets", "is_shared", "owner_id"} <= set(first)
    assert first["is_default"] is True and first["widgets"] == [] and first["widget_count"] == 0  # first = default
    assert first["owner_id"] == str(owner.pk) and first["is_shared"] is False
    second = new_dashboard(owner_client, org, title="Operations")
    assert second["is_default"] is False

    listed = owner_client.get(durl(org))
    assert listed.status_code == 200
    body = listed.json()
    assert set(body) == {"items", "next_cursor", "previous_cursor"}
    assert {i["id"] for i in body["items"]} == {first["id"], second["id"]}
    assert all(SUMMARY_KEYS <= set(i) for i in body["items"])

    r = owner_client.patch(durl(org, second["id"]), {"is_default": True}, format="json")
    assert r.status_code == 200 and r.json()["is_default"] is True
    assert owner_client.get(durl(org, first["id"])).json()["is_default"] is False  # one default per owner
    assert owner_client.patch(durl(org, first["id"]), {"title": "   "}, format="json").status_code == 400
    r = owner_client.patch(durl(org, first["id"]), {"title": "Revenue 2026", "description": ""}, format="json")
    assert r.status_code == 200 and r.json()["title"] == "Revenue 2026" and r.json()["description"] == ""

    assert owner_client.delete(durl(org, first["id"])).status_code == 204
    assert owner_client.get(durl(org, first["id"])).status_code == 404
    assert [i["id"] for i in owner_client.get(durl(org)).json()["items"]] == [second["id"]]
    with tenant_context(org.id):
        actions = set(AuditEvent.objects.filter(action__startswith="bi.dashboard").values_list("action", flat=True))
    assert {"bi.dashboard.create", "bi.dashboard.update", "bi.dashboard.delete"} <= actions


def test_widgets_crud_layout_and_bulk_positions(org, owner, owner_client, fake):
    sq = new_saved_query(owner_client, org)
    total = new_saved_query(owner_client, org, name="Total revenue", sql=TOTAL_SQL, chart=None)
    dash = new_dashboard(owner_client, org)
    chart = new_widget(owner_client, org, dash["id"], title="By region", kind="chart", saved_query_id=sq["id"],
                       layout={"x": 0, "y": 0, "w": 6, "h": 4})
    assert WIDGET_KEYS <= set(chart)
    assert chart["saved_query_id"] == sq["id"] and chart["text"] is None
    assert chart["chart"] == {"type": "bar", "x": "region", "y": ["revenue"], "series": None}  # from the query
    kpi = new_widget(owner_client, org, dash["id"], title="Total", kind="kpi", saved_query_id=total["id"])
    assert kpi["chart"] == {"type": "kpi", "x": None, "y": [], "series": None}
    assert kpi["layout"] == {"x": 0, "y": 4, "w": 6, "h": 4}  # no layout: placed below the existing tiles
    note = new_widget(owner_client, org, dash["id"], title="Notes", kind="text", text="Synthetic notes only.")
    assert note["saved_query_id"] is None and note["text"] == "Synthetic notes only."
    assert note["layout"]["y"] == 8

    detail = owner_client.get(durl(org, dash["id"])).json()
    assert detail["widget_count"] == 3 and [w["id"] for w in detail["widgets"]] == [chart["id"], kpi["id"], note["id"]]

    r = owner_client.patch(durl(org, dash["id"]), {"layout": [
        {"id": chart["id"], "x": 6, "y": 0, "w": 6, "h": 4},
        {"id": kpi["id"], "x": 0, "y": 0, "w": 6, "h": 4},
    ]}, format="json")
    assert r.status_code == 200, r.content
    assert [w["id"] for w in r.json()["widgets"]] == [kpi["id"], chart["id"], note["id"]]  # sorted by (y, x)
    unknown = owner_client.patch(durl(org, dash["id"]), {"layout": [{"id": str(uuid4()), "x": 0, "y": 0, "w": 1,
                                                                      "h": 1}]}, format="json")
    assert unknown.status_code == 400

    wurl = durl(org, dash["id"], "widgets", chart["id"])
    r = owner_client.patch(wurl, {"title": "Revenue trend", "chart": {"type": "line", "x": "region", "y": "revenue"}},
                           format="json")
    assert r.status_code == 200, r.content
    assert r.json()["title"] == "Revenue trend" and r.json()["chart"]["y"] == ["revenue"]
    assert owner_client.patch(wurl, {"kind": "text"}, format="json").status_code == 400  # a text tile has no query
    assert owner_client.patch(wurl, {"layout": {"x": 10, "y": 0, "w": 4, "h": 4}}, format="json").status_code == 400
    assert owner_client.patch(wurl, {"chart": {"type": ["bar"]}}, format="json").status_code == 400  # not a 500
    assert owner_client.delete(durl(org, dash["id"], "widgets", note["id"])).status_code == 204
    assert owner_client.get(durl(org, dash["id"])).json()["widget_count"] == 2
    assert owner_client.delete(durl(org, dash["id"], "widgets", note["id"])).status_code == 404
    with tenant_context(org.id):
        actions = set(AuditEvent.objects.filter(action__startswith="bi.dashboard.widget")
                      .values_list("action", flat=True))
    assert actions == {"bi.dashboard.widget.create", "bi.dashboard.widget.update", "bi.dashboard.widget.delete"}


@pytest.mark.parametrize("payload", [
    {"title": "Notes", "kind": "text"},  # a text tile needs text
    {"title": "Notes", "kind": "text", "text": "x", "saved_query_id": "SQ"},  # ... and no query
    {"title": "Chart", "kind": "chart"},  # a data tile needs a query
    {"title": "Chart", "kind": "chart", "saved_query_id": "SQ", "message_id": str(uuid4())},  # exactly one source
    {"title": "Chart", "kind": "map", "saved_query_id": "SQ"},
    {"title": "", "kind": "chart", "saved_query_id": "SQ"},
    {"title": "Chart", "kind": "chart", "saved_query_id": "SQ", "layout": {"x": 8, "y": 0, "w": 6, "h": 4}},
    {"title": "Chart", "kind": "chart", "saved_query_id": "SQ", "layout": {"x": 0, "y": 0, "w": True, "h": 4}},
    {"title": "Chart", "kind": "chart", "saved_query_id": "SQ", "chart": {"type": "radar"}},
    {"title": "Chart", "kind": "chart", "saved_query_id": "SQ", "chart": {"type": ["bar"]}},  # unhashable: 400, not 500
    {"title": "Chart", "kind": "chart", "saved_query_id": "SQ", "chart": {"type": {"bar": 1}}},
    {"title": "Chart", "kind": "chart", "saved_query_id": str(uuid4())},  # unknown saved query
])
def test_widget_validation(org, owner_client, fake, payload):
    sq = new_saved_query(owner_client, org)
    dash = new_dashboard(owner_client, org)
    payload = {k: (sq["id"] if v == "SQ" else v) for k, v in payload.items()}
    assert add_widget(owner_client, org, dash["id"], **payload).status_code == 400
    with tenant_context(org.id):
        assert not DashboardWidget.objects.exists()


def test_widget_count_is_capped(org, owner_client, fake):
    sq = new_saved_query(owner_client, org)
    dash = new_dashboard(owner_client, org)
    with tenant_context(org.id):
        DashboardWidget.objects.bulk_create([
            DashboardWidget(org_id=org.id, dashboard_id=dash["id"], title=f"Tile {i}", kind="text", text="t",
                            layout={"x": 0, "y": i, "w": 1, "h": 1}) for i in range(MAX_WIDGETS)])
    r = add_widget(owner_client, org, dash["id"], title="One too many", kind="chart", saved_query_id=sq["id"])
    assert r.status_code == 409 and r.json()["code"] == "too_many_widgets"


def test_saved_queries_of_others_need_sharing_and_dataset_access(org, owner, owner_client, fake):
    analyst = member_of(org, "analyst")
    analyst_client = client_for(analyst)
    private = new_saved_query(owner_client, org, name="Owner private")
    shared = new_saved_query(owner_client, org, name="Owner shared", shared=True)
    dash = new_dashboard(analyst_client, org, title="Analyst board")
    assert add_widget(analyst_client, org, dash["id"], title="x", kind="table",
                      saved_query_id=private["id"]).status_code == 400  # never reveals a private query
    widget = new_widget(analyst_client, org, dash["id"], title="Shared one", kind="table", saved_query_id=shared["id"])
    assert widget["saved_query_id"] == shared["id"]


# ================================================================================================ visibility
def test_private_dashboards_are_invisible_and_shared_ones_read_only(org, owner, owner_client, fake):
    analyst = member_of(org, "analyst")
    analyst_client = client_for(analyst)
    sq = new_saved_query(owner_client, org, shared=True)
    dash = new_dashboard(owner_client, org, title="Board")
    widget = new_widget(owner_client, org, dash["id"], title="By region", kind="chart", saved_query_id=sq["id"])
    detail, wurl = durl(org, dash["id"]), durl(org, dash["id"], "widgets", widget["id"])

    assert analyst_client.get(durl(org)).json()["items"] == []
    assert analyst_client.get(detail).status_code == 404
    assert analyst_client.patch(detail, {"title": "x"}, format="json").status_code == 404
    assert analyst_client.delete(detail).status_code == 404
    assert add_widget(analyst_client, org, dash["id"], title="x", kind="text", text="x").status_code == 404
    assert analyst_client.patch(wurl, {"title": "x"}, format="json").status_code == 404
    assert analyst_client.get(data_url(org, dash["id"], widget["id"])).status_code == 404

    assert owner_client.patch(detail, {"is_shared": True}, format="json").json()["is_shared"] is True
    assert [i["id"] for i in analyst_client.get(durl(org)).json()["items"]] == [dash["id"]]
    assert analyst_client.get(detail).status_code == 200
    assert analyst_client.get(data_url(org, dash["id"], widget["id"])).status_code == 200
    assert analyst_client.patch(detail, {"title": "x"}, format="json").status_code == 403  # only the owner edits
    assert analyst_client.delete(detail).status_code == 403
    assert add_widget(analyst_client, org, dash["id"], title="x", kind="text", text="x").status_code == 403
    assert analyst_client.patch(wurl, {"title": "x"}, format="json").status_code == 403
    assert analyst_client.delete(wurl).status_code == 403

    mine = new_dashboard(analyst_client, org, title="Analyst home")
    assert mine["is_default"] is True  # defaults are per owner
    assert mine["id"] not in {i["id"] for i in owner_client.get(durl(org)).json()["items"]}


def test_permissions(org, owner_client, fake):
    dash = new_dashboard(owner_client, org, is_shared=True)
    for role in ("viewer", "auditor"):
        client = client_for(member_of(org, role))
        assert client.get(durl(org)).status_code == 200  # dashboards.view
        assert client.get(durl(org, dash["id"])).status_code == 200
        assert client.post(durl(org), {"title": "x"}, format="json").status_code == 403  # no dashboards.manage
        assert client.patch(durl(org, dash["id"]), {"title": "x"}, format="json").status_code == 403
    anon = client_for(None)
    assert anon.get(durl(org)).status_code == 401
    assert anon.get(durl(org, dash["id"])).status_code == 401


def test_other_orgs_and_foreign_widget_ids_get_404(org, other_org, owner_client, fake):
    dash, widget, _sq = shared_dashboard_with_widget(owner_client, org)
    other = client_for(other_org_owner(other_org))
    for url in (durl(org), durl(org, dash["id"]), data_url(org, dash["id"], widget["id"])):
        assert other.get(url).status_code == 404  # not a member of org
    # IDOR through its own slug: org A's ids do not exist in org B
    assert other.get(durl(other_org, dash["id"])).status_code == 404
    assert other.get(data_url(other_org, dash["id"], widget["id"])).status_code == 404
    assert other.patch(durl(other_org, dash["id"]), {"title": "x"}, format="json").status_code == 404
    assert other.delete(durl(other_org, dash["id"])).status_code == 404
    assert add_widget(other, other_org, dash["id"], title="x", kind="text", text="x").status_code == 404
    assert other.delete(durl(other_org, dash["id"], "widgets", widget["id"])).status_code == 404
    assert other.get(durl(other_org)).json()["items"] == []
    # a widget id of another dashboard of the same org is not part of this dashboard
    second = new_dashboard(owner_client, org, title="Second")
    assert owner_client.get(data_url(org, second["id"], widget["id"])).status_code == 404
    assert owner_client.patch(durl(org, second["id"], "widgets", widget["id"]), {"title": "x"},
                              format="json").status_code == 404
    assert executions(fake) == 0  # no probe ever reached the executor


def test_dashboard_rows_are_tenant_isolated(org, other_org, owner_client, fake):
    shared_dashboard_with_widget(owner_client, org)
    with tenant_context(other_org.id):
        assert not Dashboard.objects.exists() and not DashboardWidget.objects.exists()
        with connection.cursor() as cur:
            for table in ("bi_dashboard", "bi_dashboardwidget"):
                cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed table names
                assert cur.fetchone()[0] == 0, table


# ================================================================================================ widget data
def test_widget_data_reruns_the_query_each_time(org, owner, owner_client, fake):
    dash, widget, sq = shared_dashboard_with_widget(owner_client, org)
    url = data_url(org, dash["id"], widget["id"])
    r = owner_client.get(url)
    assert r.status_code == 200, r.content
    body = r.json()
    assert DATA_KEYS <= set(body) and body["refreshed_at"]
    assert [c["name"] for c in body["columns"]] == ["region", "revenue"]
    assert body["rows"][0] == ["West", 5400.5] and body["row_count"] == 4 and body["truncated"] is False
    assert body["chart"] == {"type": "bar", "x": "region", "y": ["revenue"], "series": None}
    assert r["Cache-Control"] == "no-store"
    fake.result = ResultSet(columns=(ColumnMeta("region", "text"), ColumnMeta("revenue", "numeric")),
                            rows=(("North", Decimal("1")),), row_count=1, truncated=False, duration_ms=1)
    again = owner_client.get(url).json()
    assert again["rows"] == [["North", 1.0]] and again["row_count"] == 1  # live data, never stored rows
    assert executions(fake) == 2
    assert [c[1] for c in fake.calls if c[0] == "validate"][-2:] == [GOOD_SQL, GOOD_SQL]  # the saved SQL, guarded
    with tenant_context(org.id):
        audits = list(BiQueryAudit.objects.filter(purpose="dashboard").order_by("occurred_at", "id"))
        assert [(a.source, a.verdict, a.user_id, a.raw_sql) for a in audits] == [
            ("dashboard", "ok", owner.pk, GOOD_SQL)] * 2
        assert {str(a.id) for a in audits} == {body["query_audit_id"], again["query_audit_id"]}
        assert AccessHistory.objects.filter(query_audit_id__in=[a.id for a in audits]).count() == 2
        assert SavedQuery.objects.get(id=sq["id"]).sql == GOOD_SQL


def test_widget_data_runs_under_the_viewers_policy(org, owner, owner_client, fake):
    """The owner built the widget; a restricted viewer sees it through HIS OWN column and row policies."""
    viewer = member_of(org, "viewer")
    with tenant_context(org.id):
        viewer_role = Role.objects.get(key="viewer")
        ColumnPolicy.objects.create(dataset_id=DATASET_ID, column_name="margin", principal_type="role",
                                    principal_id=viewer_role.id, action="deny")
        ColumnPolicy.objects.create(dataset_id=DATASET_ID, column_name="customer_email", principal_type="role",
                                    principal_id=viewer_role.id, action="mask_hash")
        RowPolicy.objects.create(dataset_id=DATASET_ID, name="West only", principal_type="role",
                                 principal_id=viewer_role.id,
                                 predicate={"all": [{"col": "region", "op": "=", "value": "West"}]})
    dash, widget, _sq = shared_dashboard_with_widget(owner_client, org)
    url = data_url(org, dash["id"], widget["id"])

    assert owner_client.get(url).status_code == 200
    owner_policy = fake.policies[-1]
    assert owner_policy.user_id == owner.pk and "margin" in owner_policy.allowed_columns["sales"]
    assert not owner_policy.row_predicates and ("sales", "customer_email") not in owner_policy.column_masks

    r = client_for(viewer).get(url)
    assert r.status_code == 200, r.content
    policy = fake.policies[-1]
    assert policy.user_id == viewer.pk and policy.fingerprint != owner_policy.fingerprint
    assert "margin" not in policy.allowed_columns["sales"]
    assert policy.column_masks[("sales", "customer_email")].kind == "mask_hash"
    assert {c[0] for c in policy.row_predicates["sales"].conditions} == {"region"}
    with tenant_context(org.id):
        viewer_audit = BiQueryAudit.objects.get(id=r.json()["query_audit_id"])
        assert viewer_audit.user_id == viewer.pk and viewer_audit.source == "dashboard"
        assert viewer_audit.policy_fingerprint == policy.fingerprint


def test_widget_data_needs_query_access_to_the_dataset(org, owner_client, fake):
    analyst, viewer = member_of(org, "analyst"), member_of(org, "viewer")
    dash, widget, _sq = shared_dashboard_with_widget(owner_client, org)
    with tenant_context(org.id):  # only analysts may query the dataset now
        DatasetAcl.objects.create(dataset_id=DATASET_ID, principal_type="role",
                                  principal_id=Role.objects.get(key="analyst").id, privilege="query")
    url = data_url(org, dash["id"], widget["id"])
    assert client_for(analyst).get(url).status_code == 200
    viewer_client = client_for(viewer)
    assert viewer_client.get(durl(org, dash["id"])).status_code == 200  # the shared dashboard itself is visible
    assert viewer_client.get(url).status_code == 404  # ... its data is not
    assert owner_client.get(url).status_code == 200  # owners bypass dataset ACLs


def test_widget_data_errors(org, owner, owner_client, fake):
    dash, widget, sq = shared_dashboard_with_widget(owner_client, org)
    note = new_widget(owner_client, org, dash["id"], title="Notes", kind="text", text="Synthetic.")
    r = owner_client.get(data_url(org, dash["id"], note["id"]))
    assert r.status_code == 409 and r.json()["code"] == "widget_has_no_data"

    fake.rejections[GOOD_SQL] = ("column_not_allowed", "column margin is not allowed")
    r = owner_client.get(data_url(org, dash["id"], widget["id"]))
    assert r.status_code == 422 and r.json()["code"] == "column_not_allowed"
    with tenant_context(org.id):  # the rejected refresh stays audited (the request does not roll it back)
        audit = BiQueryAudit.objects.get(purpose="dashboard")
        assert audit.verdict == "rejected" and audit.reject_code == "column_not_allowed"
        assert str(audit.id) == r.json()["query_audit_id"]
    assert executions(fake) == 0

    fake.rejections.clear()
    assert owner_client.delete(org_url(org, f"saved-queries/{sq['id']}")).status_code == 204
    r = owner_client.get(data_url(org, dash["id"], widget["id"]))
    assert r.status_code == 409 and r.json()["code"] == "widget_query_missing"


# ================================================================================================ pinning answers
def _answer(client, org, question="revenue by region") -> dict:
    conv = client.post(org_url(org, "bi/conversations"), {"dataset_id": str(DATASET_ID)}, format="json").json()
    r = client.post(org_url(org, f"bi/conversations/{conv['id']}/messages"), {"question": question}, format="json")
    assert r.status_code == 200, r.content
    return r.json()


def test_pinning_an_answer_creates_a_saved_query_from_the_audited_sql(org, owner, owner_client, fake):
    msg = _answer(owner_client, org)
    assert msg["status"] == "ok"
    dash = new_dashboard(owner_client, org)
    validations = len(fake.policies)
    widget = new_widget(owner_client, org, dash["id"], title="Pinned revenue", kind="chart", message_id=msg["id"])
    assert len(fake.policies) == validations + 1  # re-validated under the caller's current policy
    assert widget["chart"] == {"type": "bar", "x": "region", "y": ["revenue"], "series": None}
    with tenant_context(org.id):
        sq = SavedQuery.objects.get(id=widget["saved_query_id"])
        assert (sq.name, sq.sql, sq.question, sq.owner_id, sq.dataset_id) == (
            "Pinned revenue", GOOD_SQL, "revenue by region", owner.pk, DATASET_ID)
        event = AuditEvent.objects.get(action="bi.saved_query.create", target_id=str(sq.id))
        assert event.metadata["pinned_from_message"] == msg["id"]
    again = new_widget(owner_client, org, dash["id"], title="Pinned revenue", kind="table", message_id=msg["id"])
    with tenant_context(org.id):
        assert SavedQuery.objects.get(id=again["saved_query_id"]).name == "Pinned revenue (2)"
    data = owner_client.get(data_url(org, dash["id"], widget["id"]))
    assert data.status_code == 200 and data.json()["row_count"] == 4


def test_only_own_successful_answers_can_be_pinned(org, owner_client, fake):
    analyst_client = client_for(member_of(org, "analyst"))
    owners_answer = _answer(owner_client, org)
    dash = new_dashboard(analyst_client, org)
    r = add_widget(analyst_client, org, dash["id"], title="Stolen", kind="chart", message_id=owners_answer["id"])
    assert r.status_code == 400  # another user's message does not exist for the caller
    evil = "SELECT pg_read_file('/etc/passwd')"
    fake.proposals.append(SqlProposal(sql=evil, source="llm"))
    fake.rejections[evil] = ("forbidden_function", "function pg_read_file is not allowed")
    failed = _answer(analyst_client, org)
    assert failed["status"] == "error"
    r = add_widget(analyst_client, org, dash["id"], title="Failed", kind="chart", message_id=failed["id"])
    assert r.status_code == 409 and r.json()["code"] == "pin_not_available"
    with tenant_context(org.id):
        assert not SavedQuery.objects.exists()
