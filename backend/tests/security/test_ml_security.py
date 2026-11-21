"""ML workspace security (CONTRACT "W4 additions": Safety rules for ML, Permissions; ML_ARCHITECTURE §7-§8).

Artifact safety
  * the stored artifact lives under `orgs/<org>/ml/…` and its sha256 is recorded on the version;
  * a TAMPERED artifact (bytes changed, sha256 mismatch) is refused and NEVER unpickled. The tampered bytes are a
    QA sentinel pickle (`testing.ml.sentinel_pickle`): unpickling it calls `testing.ml._pwned`, so "refused"
    means refused before deserialisation, not "failed afterwards";
  * an artifact OUTSIDE the org's own `orgs/<org>/ml/` prefix is never loaded, even with a MATCHING sha256: another
    org's key, and an upload key inside the same org (a user-uploaded pickle);
  * no ML route accepts an uploaded pickle: a multipart sentinel sent to every unsafe ML route is never unpickled
    and never becomes a model version.
Prediction API
  * an API key with scope `ml.predict` predicts in its own org; through another org it gets 404 (also with that
    org's real model id), and a key from org B gets 404 on org A's model;
  * a key without the scope gets 403;
  * more than 1,000 rows -> 400; a missing feature -> 400 that names it;
  * PredictionLog stores counts and an input hash, never the raw rows.

Real Postgres (RLS), the real fs storage backend in a tmp dir, eager task runner, Gemini faked (never live).
Every test skips cleanly until `apps.ml` routes exist.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from testing import ml
from testing.client import api_client_for

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not ml.HAS_ML_ROUTES, reason=ml.ML_SKIP_REASON),
]

ROWS = 600


@pytest.fixture(autouse=True)
def _reset_sentinel():
    ml.PWNED.clear()
    yield
    assert ml.PWNED == [], f"a QA sentinel pickle was UNPICKLED by the server: {ml.PWNED}"


@pytest.fixture
def ml_env(local_fs_storage, fake_gemini, fake_embedder, fake_redis):
    fake_gemini.respond("*", {"summary": "Synthetic narration."}, stream=["Synthetic narration."])
    return local_fs_storage


def _trained(world, capture, *, promote_to: str | None = "production") -> dict:
    """Owner ingests a churn subset, creates a project, trains the top recommendation, registers (and promotes)."""
    client = api_client_for(world.owner)
    rows = ml.churn_rows(ROWS)
    dataset_id = ml.ingest_frame(client, world.slug, rows, capture, name="Churn (QA)", filename="churn_qa.csv")
    project = ml.create_project(client, world.slug, dataset_id=dataset_id, target="churned",
                                task_type="classification")
    project_id = str(ml.pick(project, "id"))
    algorithm = ml.recommended(client, world.slug, project_id)[0]
    exp = ml.train(client, world.slug, project_id, capture, algorithm=algorithm)
    model_id, version = ml.register(client, world.slug, str(ml.pick(exp, "id")))
    if promote_to:
        resp = ml.promote(client, world.slug, model_id, version, promote_to)
        assert resp.status_code == 200, resp.content[:400]
    features = ml.project_features(project)
    sample = ml.feature_rows(rows[:5], features)
    return {"client": client, "dataset_id": dataset_id, "project": project, "model_id": model_id,
            "version": version, "features": features, "rows": rows, "sample": sample}


def _assert_refused(resp) -> None:
    """Refused cleanly: a problem+json 4xx (or 503), never 200 with predictions and never an unhandled 500."""
    assert resp.status_code != 200, f"a refused artifact still predicted: {resp.content[:300]}"
    assert 400 <= resp.status_code < 500 or resp.status_code == 503, (resp.status_code, resp.content[:300])
    assert b'"predictions"' not in resp.content


# ------------------------------------------------------------------------------------------------ artifacts
def test_artifact_is_stored_under_the_org_ml_prefix_with_its_sha256(two_orgs, ml_env,
                                                                    django_capture_on_commit_callbacks):
    from apps.core import storage

    a = two_orgs.a
    t = _trained(a, django_capture_on_commit_callbacks)
    version = ml.model_version(a, t["model_id"], t["version"])
    assert version is not None
    assert version.artifact_key.startswith(f"orgs/{a.id}/ml/"), version.artifact_key
    with a.tenant():
        data = storage.get_object(version.artifact_key)
    data = data if isinstance(data, bytes) else data.read()
    assert version.artifact_sha256 == ml.sha256_hex(data), "the recorded sha256 must be the stored bytes' sha256"
    resp = ml.predict(t["client"], a.slug, t["model_id"], t["sample"])
    assert resp.status_code == 200, resp.content[:400]
    assert len(ml.predictions(resp)) == len(t["sample"])


def test_a_tampered_artifact_is_refused_and_never_unpickled(two_orgs, ml_env, django_capture_on_commit_callbacks):
    from apps.core import storage

    a = two_orgs.a
    t = _trained(a, django_capture_on_commit_callbacks)
    version = ml.model_version(a, t["model_id"], t["version"])
    with a.tenant():  # an attacker with write access to the object store swaps the bytes
        storage.put_bytes(version.artifact_key, ml.sentinel_pickle("tampered-artifact"))
    ml.clear_ml_caches()
    _assert_refused(ml.predict(t["client"], a.slug, t["model_id"], t["sample"]))
    with a.tenant():
        ModelVersion = ml.ml_model("ModelVersion")
        assert ModelVersion.objects.get(id=version.id).artifact_sha256 == version.artifact_sha256, (
            "a refused load must not 'heal' the recorded sha256"
        )


@pytest.mark.parametrize("where", ["other_org_ml_prefix", "same_org_upload_prefix"])
def test_an_artifact_outside_the_orgs_ml_prefix_is_never_loaded_even_with_a_matching_sha(
    two_orgs, ml_env, django_capture_on_commit_callbacks, where
):
    from apps.core import storage

    a, b = two_orgs
    t = _trained(a, django_capture_on_commit_callbacks)
    version = ml.model_version(a, t["model_id"], t["version"])
    evil = ml.sentinel_pickle(f"foreign-{where}")
    if where == "other_org_ml_prefix":
        key, owner = f"orgs/{b.id}/ml/models/{uuid.uuid4()}/1.joblib", b
    else:  # a pickle a user uploaded through the normal upload path of the SAME org
        key, owner = f"orgs/{a.id}/uploads/{uuid.uuid4()}/model.joblib", a
    with owner.tenant():
        storage.put_bytes(key, evil)
    ModelVersion = ml.ml_model("ModelVersion")
    with a.tenant():
        ModelVersion.objects.filter(id=version.id).update(artifact_key=key, artifact_sha256=ml.sha256_hex(evil))
    ml.clear_ml_caches()
    _assert_refused(ml.predict(t["client"], a.slug, t["model_id"], t["sample"]))


def test_no_ml_route_accepts_an_uploaded_pickle(two_orgs, ml_env, django_capture_on_commit_callbacks):
    from testing.isolation import MissingIsolationFixture, factory_for
    from testing.routes import UNSAFE_METHODS, tenant_routes

    a = two_orgs.a
    t = _trained(a, django_capture_on_commit_callbacks)
    evil = ml.sentinel_pickle("uploaded-pickle")
    evil_sha = ml.sha256_hex(evil)
    client = t["client"]
    probed = 0
    for route in tenant_routes():
        # route.pattern is the FULL pattern ("api/v1/orgs/<slug:org>/ml/..."): look at what follows the org segment
        after_org = route.pattern.split(">/", 1)[-1].lstrip("^")
        if not after_org.startswith("ml/"):
            continue
        try:
            factory = factory_for(route)
        except MissingIsolationFixture:
            factory = None
        values = factory(a) if factory else {}
        values = {**{k: t["model_id"] for k in route.resource_kwargs}, **values}
        path = route.build(org=a.slug, **values)
        for method in (m for m in route.methods if m in UNSAFE_METHODS and m != "delete"):
            for field_name in ("file", "artifact", "model", "model_file"):
                with django_capture_on_commit_callbacks(execute=True):
                    resp = getattr(client, method)(
                        path,
                        {field_name: SimpleUploadedFile("model.joblib", evil, "application/octet-stream"),
                         "rows": "[]"},
                        format="multipart",
                    )
                assert resp.status_code < 500, f"{method.upper()} {path} ({field_name}) -> {resp.status_code}"
                probed += 1
    assert probed, "no unsafe ML route was probed"
    ModelVersion = ml.ml_model("ModelVersion")
    with a.tenant():
        assert not ModelVersion.objects.filter(artifact_sha256=evil_sha).exists(), "an uploaded pickle became a model"
    stored = [p for p in ml_env.rglob("*") if p.is_file() and evil in p.read_bytes()]
    ml_stored = [p for p in stored if "/ml/" in p.as_posix() or "\\ml\\" in str(p)]
    assert ml_stored == [], f"an uploaded pickle was stored as an ML artifact: {ml_stored}"


# ------------------------------------------------------------------------------------------------ prediction API
def _key_client(token: str) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def test_api_key_with_ml_predict_works_only_in_its_own_org(two_orgs, ml_env, django_capture_on_commit_callbacks):
    a, b = two_orgs
    ta = _trained(a, django_capture_on_commit_callbacks)
    tb = _trained(b, django_capture_on_commit_callbacks)
    key_a = _key_client(ml.api_key_for(a, a.owner, ["ml.predict"]))
    key_b = _key_client(ml.api_key_for(b, b.owner, ["ml.predict"]))

    ok = ml.predict(key_a, a.slug, ta["model_id"], ta["sample"])
    assert ok.status_code == 200, ok.content[:400]
    assert len(ml.predictions(ok)) == len(ta["sample"])

    cross = [
        ("A key, B slug, B model", key_a, b.slug, tb["model_id"]),
        ("A key, B slug, A model", key_a, b.slug, ta["model_id"]),
        ("A key, A slug, B model", key_a, a.slug, tb["model_id"]),
        ("B key, A slug, A model", key_b, a.slug, ta["model_id"]),
        ("B key, B slug, A model", key_b, b.slug, ta["model_id"]),
    ]
    for label, client, slug, model_id in cross:
        resp = ml.predict(client, slug, model_id, ta["sample"])
        assert resp.status_code == 404, f"{label}: {resp.status_code} {resp.content[:200]}"
        assert b'"predictions"' not in resp.content
    assert ml.predict(key_b, b.slug, tb["model_id"], tb["sample"]).status_code == 200


def test_api_key_without_the_ml_predict_scope_gets_403(two_orgs, ml_env, django_capture_on_commit_callbacks):
    a = two_orgs.a
    t = _trained(a, django_capture_on_commit_callbacks)
    # unrelated scopes only (API_SPECIFICATION lists `ml.predict` OR `ml.view` for predict, so ml.view is not probed)
    for scopes in (["datasets.view"], ["documents.view", "bi.chat"], ["ml.manage"]):
        token = ml.api_key_for(a, a.owner, scopes)
        resp = ml.predict(_key_client(token), a.slug, t["model_id"], t["sample"])
        assert resp.status_code == 403, f"{scopes}: {resp.status_code} {resp.content[:200]}"


def test_more_than_1000_rows_is_400(two_orgs, ml_env, django_capture_on_commit_callbacks):
    a = two_orgs.a
    t = _trained(a, django_capture_on_commit_callbacks)
    row = t["sample"][0]
    assert ml.predict(t["client"], a.slug, t["model_id"], [row] * 1000).status_code == 200, "1,000 is the limit"
    resp = ml.predict(t["client"], a.slug, t["model_id"], [row] * 1001)
    assert resp.status_code == 400, resp.content[:300]
    assert ml.predict(t["client"], a.slug, t["model_id"], []).status_code == 400, "an empty batch is invalid"


def test_a_missing_feature_is_400_and_named(two_orgs, ml_env, django_capture_on_commit_callbacks):
    a = two_orgs.a
    t = _trained(a, django_capture_on_commit_callbacks)
    missing = t["features"][0]
    row = {k: v for k, v in t["sample"][0].items() if k != missing}
    resp = ml.predict(t["client"], a.slug, t["model_id"], [row])
    assert resp.status_code == 400, resp.content[:300]
    assert missing in resp.content.decode(), f"the 400 must list the missing feature {missing!r}"


def test_prediction_log_stores_no_raw_rows(two_orgs, ml_env, django_capture_on_commit_callbacks):
    from django.forms.models import model_to_dict

    a = two_orgs.a
    t = _trained(a, django_capture_on_commit_callbacks)
    canary_spend = "98765.4321"
    rows = []
    for i, r in enumerate(t["sample"]):
        row = dict(r)
        if "monthly_spend_inr" in row:
            row["monthly_spend_inr"] = canary_spend
        if "region" in row:
            row["region"] = f"QaCanaryRegion{i}"
        rows.append(row)
    key = _key_client(ml.api_key_for(a, a.owner, ["ml.predict"]))
    assert ml.predict(key, a.slug, t["model_id"], rows).status_code == 200
    assert ml.predict(t["client"], a.slug, t["model_id"], rows).status_code == 200
    PredictionLog = ml.ml_model("PredictionLog")
    with a.tenant():
        logs = list(PredictionLog.objects.all())
    assert len(logs) >= 2, "every prediction request writes a PredictionLog"
    dumped = json.dumps([model_to_dict(log) for log in logs], default=str)
    assert canary_spend not in dumped and "98765.43" not in dumped
    assert "QaCanaryRegion" not in dumped
    for log in logs:
        values = model_to_dict(log)
        assert any(isinstance(v, int) and v == len(rows) for v in values.values()), "n_rows is recorded"
        hashes = [v for k, v in values.items() if "hash" in k or "sha256" in k]  # as built: input_sha256
        assert hashes and all(isinstance(h, str) and len(h) >= 32 for h in hashes), values
