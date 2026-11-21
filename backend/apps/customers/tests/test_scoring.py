"""Customer 360 scoring end to end on the real DB: identity resolution -> point-in-time training set (publish_frame)
-> a real churn model trained in the ML workspace -> promote -> scoring run (auto-selected model) -> CustomerScore rows
with explanations -> next-best-action. Plus CLV model compatibility, automation steps and events."""

from __future__ import annotations

import datetime as dt
import random
import sys
import types

import pytest

from apps.customers.models import CustomerProfile, CustomerScore, CustomerScoringRun
from apps.datasets.tests.helpers import csv_bytes, ingest

pytestmark = pytest.mark.django_db

N_CUSTOMERS = 48


def _data():
    rnd = random.Random(11)
    crm = [["crm_id", "full_name", "email"]]
    events = [["event_id", "customer_id", "kind", "occurred_on", "amount"]]
    base = dt.date(2026, 1, 5)
    n = 0
    for i in range(N_CUSTOMERS):
        cid = f"C-{i:03d}"
        crm.append([cid, f"Customer Number{i:03d}", f"customer{i:03d}@example.test"])
        churner = i % 3 == 0
        k = rnd.randint(1, 2) if churner else rnd.randint(3, 6)
        for _ in range(k):  # history before the label window
            day = base + dt.timedelta(days=rnd.randint(0, 70 if churner else 200))
            n += 1
            events.append([f"E-{n}", cid, "order", day.isoformat(), f"{rnd.uniform(200, 900):.2f}"])
        if churner:
            for _ in range(rnd.randint(1, 3)):
                n += 1
                day = base + dt.timedelta(days=rnd.randint(20, 120))
                events.append([f"E-{n}", cid, "ticket", day.isoformat(), ""])
        else:  # orders inside the 90-day label window
            for _ in range(rnd.randint(1, 2)):
                n += 1
                day = dt.date(2026, 8, 20) + dt.timedelta(days=rnd.randint(0, 70))
                events.append([f"E-{n}", cid, "order", day.isoformat(), f"{rnd.uniform(200, 900):.2f}"])
    n += 1
    events.append([f"E-{n}", "C-001", "order", "2026-10-31", "500.00"])  # fixes as_of
    return crm, events


def url(world, path=""):
    return f"/api/v1/orgs/{world.slug}/{path}"


@pytest.fixture
def world(two_orgs, api_client_for, fake_redis, memory_storage, django_capture_on_commit_callbacks):
    a, b = two_orgs
    client = api_client_for(a.owner)
    cap = django_capture_on_commit_callbacks
    crm, events = _data()
    _r, crm_ds = ingest(client, a, "crm.csv", csv_bytes(crm), cap, name="CRM")
    _r, ev_ds = ingest(client, a, "events.csv", csv_bytes(events), cap, name="Customer events")
    assert crm_ds["status"] == ev_ds["status"] == "ready"
    rc = client.post(url(a, "customers/sources"), {"dataset_id": crm_ds["id"], "mapping": {
        "external_id": "crm_id", "name": "full_name", "email": "email"}}, format="json")
    assert rc.status_code == 201, rc.content
    re_ = client.post(url(a, "customers/sources"), {
        "dataset_id": ev_ds["id"], "kind": "event", "link_source_id": rc.json()["id"],
        "mapping": {"customer_key": "customer_id", "occurred_at": "occurred_on", "amount": "amount",
                    "event_id": "event_id", "event_type_column": "kind"}}, format="json")
    assert re_.status_code == 201, re_.content
    with cap(execute=True):
        resp = client.post(url(a, "customers/resolve"))
    assert resp.status_code == 202
    with a.tenant():
        assert CustomerProfile.objects.count() == N_CUSTOMERS
    return a, b, client, cap


def _publish_training(a, client, cap):
    with cap(execute=True):
        resp = client.post(url(a, "customers/features/publish"), {"kind": "training"}, format="json")
    assert resp.status_code == 202, resp.content
    ds = client.get(url(a, f"datasets/{resp.json()['dataset_id']}")).json()
    assert ds["status"] == "ready", ds
    return resp.json()["dataset_id"]


def _train_and_promote(a, client, cap, dataset_id, target, features, algorithm, name):
    base = url(a, "ml")
    proj = client.post(f"{base}/projects", {"name": name, "dataset_id": dataset_id, "target": target,
                                            "features": features}, format="json")
    assert proj.status_code == 201, proj.content
    with cap(execute=True):
        resp = client.post(f"{base}/projects/{proj.json()['id']}/experiments",
                           {"algorithm": algorithm, "random_state": 3}, format="json")
    assert resp.status_code == 202, resp.content
    exp = client.get(f"{base}/experiments/{resp.json()['experiment_id']}").json()
    assert exp["status"] == "succeeded", exp
    reg = client.post(f"{base}/experiments/{exp['id']}/register", {"name": name}, format="json")
    assert reg.status_code == 201, reg.content
    model_id = reg.json()["model_id"]
    promo = client.post(f"{base}/models/{model_id}/versions/1/promote", {"stage": "staging"}, format="json")
    assert promo.status_code == 200, promo.content
    return model_id


FEATURES = ["order_count", "monetary", "recency_days", "tenure_days", "ticket_count", "event_count"]


def test_training_set_is_point_in_time(world):
    a, _b, client, cap = world
    dataset_id = _publish_training(a, client, cap)
    with a.tenant():
        from apps.datasets.services import load_current_frame

        frame = load_current_frame(dataset_id)
    assert {"churned", "future_value", "ticket_count", "recency_days"} <= set(frame.columns)
    assert "email" not in frame.columns and "full_name" not in frame.columns
    assert frame.height == N_CUSTOMERS
    churned = dict(zip(frame["profile_id"].to_list(), frame["churned"].to_list(), strict=True))
    assert sum(churned.values()) == len([i for i in range(N_CUSTOMERS) if i % 3 == 0])
    settings = client.get(url(a, "customers/settings")).json()
    assert settings["training_dataset_id"] == dataset_id and settings["churn_window_days"] == 90


def test_churn_scoring_with_a_real_model_and_nba(world):
    a, _b, client, cap = world
    dataset_id = _publish_training(a, client, cap)
    model_id = _train_and_promote(a, client, cap, dataset_id, "churned", FEATURES, "logistic_regression",
                                  "Churn (C360)")
    with cap(execute=True):
        resp = client.post(url(a, "customers/scoring-runs"), {"score_type": "churn"}, format="json")
    assert resp.status_code == 202, resp.content
    body = resp.json()
    assert body["model_id"] == model_id and body["selection"] == "auto" and body["model_version"] == 1
    run = client.get(url(a, "customers/scoring-runs?score_type=churn")).json()["items"][0]
    assert run["status"] == "succeeded", run
    assert run["stats"]["scored"] == N_CUSTOMERS and sum(run["stats"]["risk_bands"].values()) == N_CUSTOMERS
    assert sum(run["stats"]["next_best_actions"].values()) == N_CUSTOMERS
    with a.tenant():
        scores = list(CustomerScore.objects.filter(score_type="churn"))
        assert len(scores) == N_CUSTOMERS
        s = scores[0]
        assert 0 <= s.value["probability"] <= 1 and s.value["risk_band"] in ("low", "medium", "high")
        assert s.explanation["drivers"] and s.explanation["model"]["version"] == 1
        assert set(s.explanation["features"]) >= set(FEATURES)
        assert s.model_version_id is not None and s.confidence is not None
        nba_rows = CustomerScore.objects.filter(score_type="next_best_action")
        assert nba_rows.count() == N_CUSTOMERS
        with_churn = [r for r in nba_rows if r.explanation["signals"]["churn_probability"] is not None]
        assert len(with_churn) == N_CUSTOMERS
    # the explicit model id works too; a regression model is refused for churn
    with cap(execute=True):
        again = client.post(url(a, "customers/scoring-runs"), {"score_type": "churn", "model_id": model_id},
                            format="json")
    assert again.status_code == 202 and again.json()["selection"] == "explicit"
    clv_model = _train_and_promote(a, client, cap, dataset_id, "future_value", FEATURES, "linear_regression",
                                   "CLV (C360)")
    bad = client.post(url(a, "customers/scoring-runs"), {"score_type": "churn", "model_id": clv_model},
                      format="json")
    assert bad.status_code == 400 and bad.json()["code"] == "ml_model_incompatible"
    with cap(execute=True):
        clv = client.post(url(a, "customers/scoring-runs"), {"score_type": "clv"}, format="json")
    assert clv.status_code == 202 and clv.json()["model_id"] == clv_model
    with a.tenant():
        values = [s.value["predicted_value"] for s in CustomerScore.objects.filter(score_type="clv")]
        assert len(values) == N_CUSTOMERS and all(isinstance(v, float) for v in values)
        assert CustomerScoringRun.objects.filter(status="succeeded").count() == 3


@pytest.fixture
def fake_automation(monkeypatch):
    """Stand-ins for apps.automation.registry / events (Backend builds them in parallel)."""
    registered, emitted = {}, []
    registry = types.ModuleType("apps.automation.registry")
    registry.register_step = lambda kind, handler, *, schema, impact="low": registered.__setitem__(
        kind, (handler, schema, impact))
    events = types.ModuleType("apps.automation.events")
    events.emit = lambda kind, payload: emitted.append((kind, payload))
    monkeypatch.setitem(sys.modules, "apps.automation.registry", registry)
    monkeypatch.setitem(sys.modules, "apps.automation.events", events)
    import apps.automation as pkg

    monkeypatch.setattr(pkg, "registry", registry, raising=False)
    monkeypatch.setattr(pkg, "events", events, raising=False)
    return registered, emitted


def test_automation_steps_recheck_permissions(world, fake_automation):
    from apps.core.exceptions import Forbidden
    from apps.customers import automation_steps as cs
    from apps.pipelines import automation_steps as ps
    from apps.products import automation_steps as prs

    registered, _emitted = fake_automation
    cs.register()
    ps.register()
    prs.register()
    assert {"customers.resolve", "customers.score", "pipelines.run", "products.normalize"} <= set(registered)
    a, _b, _client, cap = world
    owner_ctx = types.SimpleNamespace(actor_id=a.owner.pk)
    viewer_ctx = types.SimpleNamespace(actor_id=a.users["viewer"].pk)
    with a.tenant():
        with pytest.raises(Forbidden):
            cs.score_step(viewer_ctx, {"score_type": "next_best_action"})
        with pytest.raises(Forbidden):
            prs.normalize_step(viewer_ctx, {})
        with pytest.raises(Forbidden):
            cs.resolve_step(types.SimpleNamespace(), {})
        with cap(execute=True):
            out = cs.score_step(owner_ctx, {"score_type": "next_best_action"})
    assert out["score_type"] == "next_best_action" and out["job_id"]
    with a.tenant():
        assert CustomerScoringRun.objects.get(id=out["run_id"]).status == "succeeded"


def test_ingest_finished_event_is_emitted(two_orgs, api_client_for, fake_redis, memory_storage,
                                          django_capture_on_commit_callbacks, fake_automation):
    _registered, emitted = fake_automation
    a = two_orgs.a
    client = api_client_for(a.owner)
    _r, ds = ingest(client, a, "x.csv", csv_bytes([["a", "b"], ["1", "2"]]), django_capture_on_commit_callbacks,
                    name="Event probe")
    kinds = [(k, p) for k, p in emitted if k == "dataset.ingest_finished" and p["dataset_id"] == ds["id"]]
    assert kinds, emitted
    payload = kinds[-1][1]
    assert payload["ok"] is True and payload["status"] == "succeeded" and payload["org_id"] == str(a.org.id)
    assert payload["dataset_name"] == "Event probe" and payload["row_count"] == 1
