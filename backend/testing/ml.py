"""ML workspace test flows (CONTRACT "W4 additions"; API_SPECIFICATION Phase 9; ML_ARCHITECTURE).

Public API only (plus ORM reads of `apps.ml.models` for the safety checks). Payload shapes are read tolerantly:
the routes and permissions are binding, the field names of the JSON bodies are the AI/ML agent's as-built
choice, so every accessor below accepts the documented alternatives and FAILS with the payload when none match
(never a silent pass). Everything here needs the database except the pure helpers at the top.

Synthetic data only: the frames are derived from `testdata/generated/churn.csv` (QA-owned generator).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import pickle
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .client import tenant_route_exists

REPO = Path(__file__).resolve().parents[2]
CHURN_CSV = REPO / "testdata" / "generated" / "churn.csv"

HAS_ML_ROUTES = tenant_route_exists("ml/projects")
ML_SKIP_REASON = "/orgs/<org>/ml/projects not routed yet (AI/ML W4 apps.ml)"


# ------------------------------------------------------------------------------------------------ pickle sentinel
PWNED: list[str] = []


def _pwned(marker: str) -> str:
    """Called ONLY if somebody unpickles a QA sentinel. Must never run."""
    PWNED.append(marker)
    return marker


class _Sentinel:
    def __init__(self, marker: str):
        self.marker = marker

    def __reduce__(self):
        return (_pwned, (self.marker,))


def sentinel_pickle(marker: str) -> bytes:
    """A pickle (also a valid `joblib.load` input) whose loading calls `_pwned(marker)` instead of building a model."""
    return pickle.dumps(_Sentinel(marker), protocol=4)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------------------------------------ synthetic frames
def churn_rows(limit: int | None = None) -> list[dict[str, str]]:
    with CHURN_CSV.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows[:limit] if limit else rows


def to_csv(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def leaky_churn_rows(limit: int = 1200) -> list[dict[str, Any]]:
    """churn.csv plus leakage hazards the guard must drop: an ID-like column with an `_id` suffix
    (`customer_id`, already present), an all-unique integer row number (`row_no`), an exact copy of the target
    (`churn_flag`) and a near-duplicate of it (`churn_score_post`: |corr| > 0.98)."""
    out = []
    for i, r in enumerate(churn_rows(limit)):
        y = int(r["churned"])
        out.append(
            {
                **r,
                "row_no": 100000 + i,
                "churn_flag": y,
                "churn_score_post": round(y * 0.97 + (0.01 if i % 2 else 0.0), 4),
            }
        )
    return out


# ------------------------------------------------------------------------------------------------ payload readers
def pick(body: Any, *paths: str, default: Any = ...) -> Any:
    """The first present value among dotted `paths` ("experiment.id", "id"). Fails loudly when none exists."""
    for path in paths:
        cur = body
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
                cur = cur[int(part)]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    if default is not ...:
        return default
    raise AssertionError(f"none of {paths} in payload: {json.dumps(body, default=str)[:600]}")


def column_names(value: Any) -> list[str]:
    """Feature lists may be ["a", "b"] or [{"name": "a", ...}] or {"a": {...}}."""
    if value is None:
        return []
    if isinstance(value, dict):
        if "columns" in value or "features" in value:
            return column_names(value.get("columns") or value.get("features"))
        return [str(k) for k in value]
    names = []
    for item in value:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("column") or item.get("column_name")))
        else:
            names.append(str(item))
    return names


def project_features(project: dict) -> list[str]:
    return column_names(pick(project, "features", "feature_columns", "feature_list", "columns"))


def excluded_by_policy(project: dict) -> list[str]:
    return column_names(pick(project, "excluded_by_policy", "policy.excluded_by_policy", default=[]))


def leakage_dropped(project: dict) -> list[str]:
    """Columns the leakage guard dropped (any of the reasonable keys; empty when none is present)."""
    for key in ("leakage", "dropped_columns", "dropped", "leakage_dropped", "excluded_by_leakage", "leakage_guard"):
        value = project.get(key)
        if value:
            if isinstance(value, dict) and "dropped" in value:
                value = value["dropped"]
            return column_names(value)
    return []


def holdout_metrics(experiment: dict) -> dict:
    """The held-out test-set metrics of an experiment."""
    metrics = pick(experiment, "metrics")
    for key in ("test", "holdout", "test_metrics"):
        if isinstance(metrics.get(key), dict):
            return metrics[key]
    return metrics


def metric(metrics: dict, *names: str) -> float:
    for n in names:
        if n in metrics and metrics[n] is not None:
            return float(metrics[n])
    raise AssertionError(f"none of {names} in metrics {sorted(metrics)}")


def confusion(experiment: dict) -> tuple[list[Any], list[list[int]]]:
    """(labels, matrix rows=true, cols=predicted) from the experiment (top level or inside metrics.test)."""
    raw = pick(
        experiment,
        "confusion_matrix",
        "metrics.test.confusion_matrix",
        "metrics.confusion_matrix",
        "details.confusion_matrix",
    )
    if isinstance(raw, dict):
        matrix = pick(raw, "matrix", "values", "data")
        labels = raw.get("labels") or list(range(len(matrix)))
    else:
        matrix, labels = raw, list(range(len(raw)))
    return list(labels), [[int(v) for v in row] for row in matrix]


# ------------------------------------------------------------------------------------------------ API flows
def ingest_frame(client, slug: str, rows: list[dict[str, Any]], capture, *, name: str, filename: str) -> str:
    from .flows import upload_and_ingest

    dataset = upload_and_ingest(client, slug, filename, to_csv(rows), capture, name=name)
    return str(dataset["id"])


def ml_base(slug: str) -> str:
    return f"/api/v1/orgs/{slug}/ml"


def create_project(client, slug: str, *, dataset_id: str, target: str, task_type: str | None = None,
                   features: list[str] | None = None, name: str = "QA project", expect: int = 201):
    body: dict[str, Any] = {"name": name, "dataset_id": dataset_id, "target": target}
    if task_type:
        body["task_type"] = task_type
    if features is not None:
        body["features"] = features
    resp = client.post(f"{ml_base(slug)}/projects", body, format="json")
    assert resp.status_code == expect, (resp.status_code, resp.content[:600])
    return resp.json() if resp.content else {}


def recommended(client, slug: str, project_id: str) -> list[str]:
    """Algorithm keys from GET …/recommendations, best first (ML_ARCHITECTURE §5: ranked, with reasons)."""
    resp = client.get(f"{ml_base(slug)}/projects/{project_id}/recommendations")
    assert resp.status_code == 200, resp.content[:600]
    body = resp.json()
    items = body if isinstance(body, list) else pick(body, "items", "recommendations", "candidates")
    keys = [str(pick(i, "algorithm", "key", "model", "name")) for i in items]
    assert keys, f"no recommendations: {body}"
    for i in items:
        assert str(pick(i, "reason", "reasons", "why", default="")).strip(), f"recommendation without a reason: {i}"
    return keys


def train(client, slug: str, project_id: str, capture, *, algorithm: str, random_state: int = 7,
          **extra) -> dict:
    """POST …/experiments (202 + job; eager runner in tests) and return the finished experiment detail."""
    body = {"algorithm": algorithm, "random_state": random_state, **extra}
    with capture(execute=True):
        resp = client.post(f"{ml_base(slug)}/projects/{project_id}/experiments", body, format="json")
    assert resp.status_code in (201, 202), (resp.status_code, resp.content[:600])
    exp_id = pick(resp.json(), "experiment.id", "experiment_id", "id")
    detail = client.get(f"{ml_base(slug)}/experiments/{exp_id}")
    assert detail.status_code == 200, detail.content[:600]
    exp = detail.json()
    assert pick(exp, "status") == "succeeded", json.dumps(exp, default=str)[:800]
    return exp


def parse_registration(body: dict) -> tuple[str, Any]:
    """(model_id, version) from a register response; version is the {v} path value of the promote route."""
    model_id = pick(body, "model_id", "model.id", "version.model_id")
    version = pick(body, "version.version", "version_number", "version", "versions.-1.version", "number")
    if isinstance(version, dict):
        version = pick(version, "version", "number", "id")
    return str(model_id), version


def register(client, slug: str, experiment_id: str, **body) -> tuple[str, Any]:
    """POST …/experiments/{id}/register -> (model_id, version)."""
    resp = client.post(f"{ml_base(slug)}/experiments/{experiment_id}/register", body, format="json")
    assert resp.status_code in (200, 201), (resp.status_code, resp.content[:600])
    return parse_registration(resp.json())


def promote(client, slug: str, model_id: str, version, stage: str = "production"):
    return client.post(
        f"{ml_base(slug)}/models/{model_id}/versions/{version}/promote", {"stage": stage}, format="json"
    )


def predict(client, slug: str, model_id: str, rows: list[dict[str, Any]], *, explain: bool = False, **headers):
    body: dict[str, Any] = {"rows": rows}
    if explain:
        body["explain"] = True
    return client.post(f"{ml_base(slug)}/models/{model_id}/predict", body, format="json", **headers)


def predictions(resp) -> list[Any]:
    return list(pick(resp.json(), "predictions", "results", "rows"))


def feature_rows(rows: Iterable[dict[str, Any]], features: Iterable[str]) -> list[dict[str, Any]]:
    keep = list(features)
    return [{k: r[k] for k in keep if k in r} for r in rows]


# ------------------------------------------------------------------------------------------------ ORM (apps.ml)
def ml_model(name: str):
    from django.apps import apps as django_apps

    return django_apps.get_model("ml", name)


def model_version(world, model_id: str, version=None):
    """The ModelVersion row (artifact_key / artifact_sha256) of `model_id` inside the world's tenant."""
    ModelVersion = ml_model("ModelVersion")
    fk = next(f.name for f in ModelVersion._meta.get_fields() if getattr(f, "related_model", None)
              is ml_model("MLModel") and f.many_to_one)
    with world.tenant():
        qs = ModelVersion.objects.filter(**{f"{fk}_id": model_id})
        if version is not None and str(version).isdigit():
            num = next((f for f in ("version", "number", "version_number")
                        if f in {x.name for x in ModelVersion._meta.get_fields()}), None)
            if num:
                qs = qs.filter(**{num: int(version)})
        elif version is not None:
            qs = qs.filter(id=version)
        return qs.order_by("-created_at").first()


def clear_ml_caches() -> int:
    """Clear every in-process model cache in apps.ml (LRU dicts, functools caches) so the next predict re-loads
    the artifact from storage. Returns how many caches were cleared."""
    import sys

    cleared = 0
    for name, module in list(sys.modules.items()):
        if not name.startswith("apps.ml") or module is None:
            continue
        for attr, value in list(vars(module).items()):
            if hasattr(value, "cache_clear"):
                value.cache_clear()
                cleared += 1
            elif "cache" in attr.lower() and hasattr(value, "clear") and not isinstance(value, type):
                value.clear()
                cleared += 1
    return cleared


def api_key_for(world, user, scopes: list[str]) -> str:
    """A real Nexora API key (Bearer token) for `user` in `world`'s org with `scopes`."""
    from apps.accounts.services import create_api_key

    with world.tenant():
        _key, token = create_api_key(name=f"qa-{'-'.join(scopes) or 'none'}", scopes=scopes, created_by=user)
    return token


def bearer(token: str) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def approval_id_of(resp) -> str | None:
    """The approval id of a 202 promotion (CONTRACT "W5 additions / Approvals"), else None."""
    if resp.status_code != 202:
        return None
    body = resp.json() if resp.content else {}
    value = pick(body, "approval_id", "approval.id", default=None)
    return str(value) if value else None


def promote_effective(world, client, model_id: str, version, stage: str = "production"):
    """Promote and, when the promotion is approval-gated (202 + approval_id, W5), approve it as the org's ADMIN
    (a second person holding `ml.promote`) with password re-authentication. Returns the final response: the
    promote response (200) or the decision response."""
    from .client import api_client_for
    from .factories import TEST_PASSWORD

    resp = promote(client, world.slug, model_id, version, stage)
    approval_id = approval_id_of(resp)
    if approval_id is None:
        return resp
    from .automation import decide

    return decide(api_client_for(world.user("admin")), world.slug, approval_id, "approve", password=TEST_PASSWORD)
