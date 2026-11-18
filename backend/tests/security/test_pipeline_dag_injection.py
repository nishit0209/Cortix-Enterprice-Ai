"""Pipeline DAG injection (DATA_ENGINEERING_DESIGN §5: JSON-Schema-validated DAG, structured predicates, safe
expression subset validated by sqlglot, typed steps only).

Every hostile DAG must be refused — at `POST /pipelines` (400/422) or, at the latest, when a run is requested
(4xx or a failed run) — and must never execute: no new dataset version, no object outside the org prefix, no
`..` in any stored key, never a 500. A benign DAG and a benign derive expression are accepted (positive controls,
so a rejection proves the CHECK, not a schema typo). Cross-org dataset ids inside the DAG are IDOR attempts.
Skips while the pipelines API is not merged.
"""

from __future__ import annotations

import copy
import json

import pytest

from testing.client import api_client_for, requires_tenant_route, route_exists
from testing.flows import job_outcome, upload_and_ingest

pytestmark = [pytest.mark.django_db, requires_tenant_route("pipelines", "Data Engineering")]
SAMPLE = b"order_id,region,amount\n1,North,10.5\n2,South,20\n3,West,7.25\n"


@pytest.fixture
def pipelines(two_orgs, memory_storage, fake_redis, fake_embedder, django_capture_on_commit_callbacks):
    a, b = two_orgs
    base = f"/api/v1/orgs/{a.slug}/pipelines"
    if not route_exists(base):
        pytest.skip("pipelines API not implemented yet (Data Engineering, wave 2)")
    client = api_client_for(a.owner)
    ds_a = upload_and_ingest(client, a.slug, "orders.csv", SAMPLE, django_capture_on_commit_callbacks, name="Orders")
    ds_b = upload_and_ingest(api_client_for(b.owner), b.slug, "orders.csv", SAMPLE,
                             django_capture_on_commit_callbacks, name="Beta orders")

    def versions(dataset_id: str) -> int:
        resp = client.get(f"/api/v1/orgs/{a.slug}/datasets/{dataset_id}/versions")
        body = resp.json()
        return len(body["items"] if isinstance(body, dict) else body)

    def submit(dag: dict, name: str = "QA pipeline") -> tuple[str, int, str | None]:
        """-> ("rejected"|"run_refused"|"run_failed"|"ran", http status, code)."""
        before = versions(ds_a["id"])
        created = client.post(base, {"name": name, "dag": dag}, format="json")
        assert created.status_code < 500, created.content[:300]
        if created.status_code >= 400:
            return "rejected", created.status_code, created.json().get("code")
        assert created.status_code == 201, created.content[:300]
        pid = created.json()["id"]
        with django_capture_on_commit_callbacks(execute=True):
            run = client.post(f"{base}/{pid}/runs", {}, format="json")
        assert run.status_code < 500, run.content[:300]
        if run.status_code >= 400:
            return "run_refused", run.status_code, run.json().get("code")
        job = job_outcome(client, a.slug, run.json()["job_id"])
        if job["status"] == "failed":
            assert versions(ds_a["id"]) == before, "a failed run must not publish a dataset version"
            return "run_failed", 202, (job.get("error") or {}).get("code")
        return "ran", 202, None

    def baseline() -> dict:
        return {
            "nodes": [
                {"key": "src", "type": "source.dataset", "config": {"dataset_id": ds_a["id"], "version": "current"}},
                {"key": "s1", "type": "transform.rename", "config": {"map": {"amount": "amount_inr"}}},
                {"key": "dst", "type": "destination.dataset",
                 "config": {"dataset_id": ds_a["id"], "mode": "new_version"}},
            ],
            "edges": [["src", "s1"], ["s1", "dst"]],
        }

    submit.baseline, submit.world, submit.other, submit.ds_a, submit.ds_b = baseline, a, b, ds_a, ds_b
    submit.storage = memory_storage
    return submit


def _with_step(dag: dict, step: dict) -> dict:
    dag = copy.deepcopy(dag)
    dag["nodes"].insert(2, step)
    dag["edges"] = [["src", "s1"], ["s1", step["key"]], [step["key"], "dst"]]
    return dag


def _derive(expression: str) -> dict:
    return {"key": "d1", "type": "transform.derive", "config": {"column": "derived", "expression": expression}}


def _refused(outcome: tuple[str, int, str | None]) -> bool:
    return outcome[0] in ("rejected", "run_refused", "run_failed")


def test_positive_controls_are_accepted(pipelines):
    assert pipelines(pipelines.baseline())[0] == "ran"
    assert pipelines(_with_step(pipelines.baseline(), _derive("amount * 2")))[0] == "ran"


HOSTILE_EXPRESSIONS = [
    "__import__('os').system('id')",
    "eval('1+1')",
    "amount.__class__.__mro__",
    "pg_sleep(10)",
    "(SELECT email FROM customers)",
    "amount; DROP TABLE customers",
    "pl.col('amount').map_elements(eval)",
    "lambda x: x",
    "current_setting('app.org_id')",
    "amount /* */ + (SELECT 1)",
]


@pytest.mark.parametrize("expression", HOSTILE_EXPRESSIONS)
def test_hostile_derive_expressions_never_run(pipelines, expression):
    outcome = pipelines(_with_step(pipelines.baseline(), _derive(expression)))
    assert _refused(outcome), f"{expression!r} was executed: {outcome}"


@pytest.mark.parametrize(
    "step",
    [
        {"key": "f1", "type": "transform.filter", "config": {"predicate": "1=1; DROP TABLE customers"}},
        {"key": "f1", "type": "transform.filter", "config": {"sql": "region = 'North' OR 1=1"}},
        {"key": "p1", "type": "python.exec", "config": {"code": "import os; os.system('id')"}},
        {"key": "p1", "type": "sql.raw", "config": {"sql": "SELECT * FROM pg_shadow"}},
        {"key": "p1", "type": "../transform.rename", "config": {"map": {}}},
        {"key": "p1", "type": "transform.rename", "config": {"map": {"amount": "x; DROP TABLE y"}}},
    ],
    ids=["filter-raw-sql", "filter-sql-key", "python-exec", "sql-raw", "type-traversal", "rename-to-sql"],
)
def test_untyped_or_raw_steps_never_run(pipelines, step):
    outcome = pipelines(_with_step(pipelines.baseline(), step))
    assert _refused(outcome), f"{json.dumps(step)} was executed: {outcome}"


def test_graph_structure_violations_are_rejected(pipelines):
    base = pipelines.baseline()
    cycle = copy.deepcopy(base)
    cycle["edges"].append(["dst", "src"])
    dangling = copy.deepcopy(base)
    dangling["edges"].append(["s1", "nowhere"])
    two_dst = copy.deepcopy(base)
    two_dst["nodes"].append({"key": "dst2", "type": "destination.dataset",
                             "config": {"dataset_id": pipelines.ds_a["id"], "mode": "new_version"}})
    two_dst["edges"].append(["s1", "dst2"])
    huge = {"nodes": [{"key": f"n{i}", "type": "transform.rename", "config": {"map": {}}} for i in range(5000)],
            "edges": [[f"n{i}", f"n{i + 1}"] for i in range(4999)]}
    for name, dag in (("cycle", cycle), ("dangling", dangling), ("two destinations", two_dst), ("huge", huge)):
        outcome = pipelines(dag)
        assert outcome[0] == "rejected" and outcome[1] in (400, 413, 422), f"{name}: {outcome}"


@pytest.mark.ac("P2-AC2")
def test_dag_cannot_reference_another_orgs_dataset(pipelines):
    other = pipelines.ds_b["id"]
    for node_key in ("src", "dst"):
        dag = pipelines.baseline()
        node = next(n for n in dag["nodes"] if n["key"] == node_key)
        node["config"]["dataset_id"] = other
        outcome = pipelines(dag)
        assert outcome[0] in ("rejected", "run_refused", "run_failed"), f"{node_key} -> org B dataset: {outcome}"
        assert outcome[1] in (400, 404, 422, 202), outcome


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "..\\..\\orgs\\other\\x", "orgs/00000000-0000-0000-0000-000000000000/x", "\x00evil"]
)
def test_traversal_in_names_never_escapes_the_org_prefix(pipelines, name):
    dag = pipelines.baseline()
    dag["nodes"][1]["key"] = "../../s1"
    dag["edges"] = [["src", "../../s1"], ["../../s1", "dst"]]
    other_before = pipelines.storage.keys_for(pipelines.other.id)
    outcome = pipelines(dag, name=name)
    assert outcome[1] < 500
    assert pipelines.storage.keys_for(pipelines.other.id) == other_before, "pipeline wrote into org B's prefix"
    allowed = (f"orgs/{pipelines.world.id}/", f"orgs/{pipelines.other.id}/")
    for key in pipelines.storage.objects:
        assert key.startswith(allowed), f"object outside every org prefix: {key}"
        assert ".." not in key.split("/") and "\\" not in key and "\x00" not in key, key
