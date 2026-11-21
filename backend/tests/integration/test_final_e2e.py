"""Final end-to-end flow on the real database (W5 QA mission C).

One demo org, every product area chained through the PUBLIC API, exactly as a customer would use it:

  1. `manage.py seed_demo` -> the demo login sees ready, synthetic datasets;
  2. a Conversational BI question ("total revenue by region") answers through the guarded builder;
  3. the answer is pinned as a dashboard widget whose data re-executes (rows, refreshed_at);
  4. a churn model is trained on the seeded churn dataset, registered, and promoted to production
     (W5: 202 + approval, approved by a second admin with password re-auth);
  5. Customer 360: the seeded customers become profiles, their features are published as a dataset, a customer
     churn model is trained on it (promotion approved) and a scoring run writes churn scores;
  6. an automation workflow (bi.saved_query on the pinned widget's saved query -> notify) runs to `succeeded`.

Each stage asserts its own outcome; a stage whose routes are not built yet skips the REST of the flow with the
stage named in the reason (so a partial build never reports green for what it does not have).
Transactional (the command runs in autocommit like production). Gemini is the gated fake; no network.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command, get_commands

from testing import automation as au
from testing import ml
from testing.client import api_client_for, tenant_route_exists
from testing.factories import add_member, make_user
from testing.flows import find_seeded_dataset, items

DEMO_EMAIL = "demo-e2e@example.test"
DEMO_PASSWORD = "Synthetic-Demo-Passw0rd!"

pytestmark = [
    pytest.mark.skipif("seed_demo" not in get_commands(), reason="manage.py seed_demo not implemented"),
    pytest.mark.django_db(transaction=True, databases="__all__"),
]


@pytest.fixture
def runtime(settings, tmp_path, fake_embedder, gated_fake_gemini, network_spy):
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = tmp_path / "storage"
    settings.JOB_EVENTS_BACKEND = "db"
    gated_fake_gemini.respond("*", {"summary": "Synthetic narration.", "steps": []}, stream=["Synthetic narration."])
    return gated_fake_gemini


class _World:
    """The seeded org seen through the testing.ml / testing.automation helpers (slug, tenant, extra members)."""

    def __init__(self, org, owner):
        self.org, self.users, self.resources = org, {"owner": owner}, {}

    @property
    def id(self):
        return self.org.id

    @property
    def slug(self) -> str:
        return self.org.slug

    @property
    def owner(self):
        return self.users["owner"]

    def user(self, role_key: str):
        if role_key not in self.users:
            u = make_user(f"e2e-{role_key}@example.test")
            add_member(self.org, u, role_key)
            self.users[role_key] = u
        return self.users[role_key]

    def tenant(self):
        from testing.factories import OrgWorld

        return OrgWorld.tenant(self)


def _ok(resp, *codes):
    assert resp.status_code in (codes or (200,)), (resp.status_code, resp.content[:500])
    return resp.json() if resp.content else {}


def test_seed_to_bi_to_dashboard_to_ml_to_c360_to_automation(runtime, django_capture_on_commit_callbacks):
    from apps.accounts.models import User
    from apps.orgs.models import Organization

    capture = django_capture_on_commit_callbacks
    out = io.StringIO()
    call_command("seed_demo", "--email", DEMO_EMAIL, "--password", DEMO_PASSWORD, "--org-name", "E2E Demo",
                 stdout=out)
    user = User.objects.get(email=DEMO_EMAIL)
    client = api_client_for(user)
    slug = _ok(client.get("/api/v1/me"))["orgs"][0]["slug"]
    world = _World(Organization.objects.using("ops").get(slug=slug), user)
    base = f"/api/v1/orgs/{slug}"

    # 1. seeded datasets ------------------------------------------------------------------------------------------
    datasets = items(client.get(f"{base}/datasets"))
    sales, churn = find_seeded_dataset(datasets, "sales.csv"), find_seeded_dataset(datasets, "churn.csv")
    customers = find_seeded_dataset(datasets, "customers.csv")
    assert {sales["status"], churn["status"], customers["status"]} == {"ready"}

    # 2. BI question --------------------------------------------------------------------------------------------------
    conv = _ok(client.post(f"{base}/bi/conversations", {"dataset_id": sales["id"], "title": "E2E"}, format="json"),
               201)
    msg = _ok(client.post(f"{base}/bi/conversations/{conv['id']}/messages", {"question": "total revenue by region"},
                          format="json", HTTP_ACCEPT="application/json"))
    assert msg["sql"]["source"] == "builder" and msg["result"]["rows"], msg

    # 3. dashboard widget -------------------------------------------------------------------------------------------
    board = _ok(client.post(f"{base}/dashboards", {"title": "E2E board"}, format="json"), 201)
    widget = _ok(client.post(f"{base}/dashboards/{board['id']}/widgets",
                             {"title": "Revenue by region", "kind": "table", "message_id": msg["id"],
                              "layout": {"x": 0, "y": 0, "w": 6, "h": 4}}, format="json"), 201)
    data = _ok(client.get(f"{base}/dashboards/{board['id']}/widgets/{widget['id']}/data"))
    assert data["row_count"] > 0 and data.get("refreshed_at"), data
    saved_query_id = widget.get("saved_query_id")
    assert saved_query_id

    # 4. ML: train churn, register, promote to production (approval-gated in W5) ----------------------------------
    if not ml.HAS_ML_ROUTES:
        pytest.skip("stage 4 (ML) not routed")
    project = ml.create_project(client, slug, dataset_id=str(churn["id"]), target="churned",
                                task_type="classification", name="E2E churn")
    project_id = str(ml.pick(project, "id"))
    exp = ml.train(client, slug, project_id, capture, algorithm=ml.recommended(client, slug, project_id)[0])
    model_id, version = ml.register(client, slug, str(ml.pick(exp, "id")))
    with capture(execute=True):
        promoted = ml.promote_effective(world, client, model_id, version, "production")
    assert promoted.status_code == 200, promoted.content[:400]
    assert ml.model_version(world, model_id, version).stage == "production"
    sample = [{k: v for k, v in r.items() if k not in ("churned", "customer_id")} for r in ml.churn_rows(5)]
    assert len(ml.predictions(ml.predict(client, slug, model_id, sample))) == 5

    # 5. Customer 360: profiles + churn scores ------------------------------------------------------------------------
    if not tenant_route_exists("customers/scoring-runs"):
        pytest.skip("stage 5 (Customer 360 scoring-runs) not routed")
    mapping = {"external_id": "customer_id", "name": "customer_name", "email": "email", "phone": "phone",
               "address": "city", "created_at": "signup_date", "dob": "date_of_birth"}
    src = _ok(client.post(f"{base}/customers/sources", {"dataset_id": str(customers["id"]), "name": "E2E customers",
                                                        "mapping": mapping}, format="json"), 200, 201)
    # the seeded orders become order events (the training set's labels come from order history)
    orders = find_seeded_dataset(datasets, "orders.csv")
    _ok(client.post(f"{base}/customers/sources", {
        "dataset_id": str(orders["id"]), "kind": "event", "link_source_id": str(src["id"]),
        "mapping": {"customer_key": "customer_id", "occurred_at": "order_date", "amount": "amount",
                    "event_id": "order_id", "event_type": "order", "channel": "channel"}}, format="json"), 200, 201)
    with capture(execute=True):
        _ok(client.post(f"{base}/customers/resolve", {}, format="json"), 200, 202)
    profiles = items(client.get(f"{base}/customers"))
    assert profiles, "resolution produced no customer profiles"
    # the "customer project": a churn model trained on the point-in-time Customer 360 TRAINING set (kind=training:
    # features as of the cutoff + `churned` / `future_value` labels), so the scoring run feeds it the same features
    with capture(execute=True):
        published = _ok(client.post(f"{base}/customers/features/publish", {"kind": "training"}, format="json"),
                        200, 201, 202)
    features_ds = str(published["dataset_id"])
    columns = [c["name"] for c in _ok(client.get(f"{base}/datasets/{features_ds}/columns"))]
    assert {"churned", "future_value"} <= set(columns), f"training set without labels: {columns}"
    label = "churned"
    # never feed the other label (future_value) or the id to the churn model
    c_features = [c for c in columns if c not in ("churned", "future_value", "profile_id")]
    c_project = ml.create_project(client, slug, dataset_id=features_ds, target=label, task_type="classification",
                                  features=c_features, name="E2E customer churn")
    c_project_id = str(ml.pick(c_project, "id"))
    c_exp = ml.train(client, slug, c_project_id, capture,
                     algorithm=ml.recommended(client, slug, c_project_id)[0])
    c_model_id, c_version = ml.register(client, slug, str(ml.pick(c_exp, "id")))
    with capture(execute=True):
        assert ml.promote_effective(world, client, c_model_id, c_version, "production").status_code == 200
    with capture(execute=True):
        _ok(client.post(f"{base}/customers/scoring-runs", {"score_type": "churn", "model_id": c_model_id},
                        format="json"), 200, 201, 202)
    from django.apps import apps as django_apps

    CustomerScore = django_apps.get_model("customers", "CustomerScore")
    with world.tenant():
        scored = CustomerScore.objects.count()
    assert scored > 0, "the scoring run wrote no CustomerScore"
    scores = client.get(f"{base}/customers/{profiles[0]['id']}/scores")
    assert scores.status_code == 200, scores.content[:300]

    # 6. automation: run the pinned query, then notify ------------------------------------------------------------
    if not au.HAS_WORKFLOWS:
        pytest.skip("stage 6 (automation) not routed")
    wf = au.create_workflow(client, slug, [
        au.step("bi.saved_query", "q", {"saved_query_id": str(saved_query_id)}),
        au.step("notify", "n", {"title": "E2E", "body": "E2E revenue refreshed"}),
    ])
    resp = au.start_run(client, slug, str(wf["id"]), capture)
    assert resp.status_code in au.OK, resp.content[:400]
    run_id = au.run_id_of(resp.json())
    assert au.run_status(client, slug, run_id) == "succeeded", au.run_detail(client, slug, run_id)
    steps = au.run_steps(client, slug, run_id)
    assert len(steps) == 2 and all(str(ml.pick(s, "status")) == "succeeded" for s in steps), steps
