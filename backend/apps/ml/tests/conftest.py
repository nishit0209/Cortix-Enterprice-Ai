"""Shared fixtures of the apps.ml DB tests: a synthetic churn dataset ingested through the real upload path."""

from __future__ import annotations

import random

import pytest

from apps.datasets.tests.helpers import _cleanup_test_warehouse, csv_bytes, ingest  # noqa: F401


def _churn_rows(n: int = 240) -> list[list]:
    rnd = random.Random(7)
    rows = [["customer_id", "age", "plan", "monthly_spend", "tenure_months", "churned"]]
    for i in range(n):
        plan = rnd.choice(["basic", "plus", "pro"])
        tenure = rnd.randint(1, 60)
        spend = round(rnd.uniform(10, 200), 2)
        risk = 0.7 if tenure < 12 else 0.15
        risk += 0.15 if plan == "basic" else 0.0
        rows.append([10_000 + i, rnd.randint(18, 75), plan, spend, tenure, int(rnd.random() < risk)])
    return rows


@pytest.fixture
def env(two_orgs, api_client_for, memory_storage, django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner = api_client_for(a.owner)
    _resp, dataset = ingest(owner, a.org, "churn.csv", csv_bytes(_churn_rows()), django_capture_on_commit_callbacks,
                            name="Churn training")
    assert dataset["status"] == "ready", dataset

    class Env:
        pass

    e = Env()
    e.a, e.b, e.owner, e.dataset_id, e.capture = a, two_orgs.b, owner, dataset["id"], django_capture_on_commit_callbacks
    e.base = f"/api/v1/orgs/{a.slug}/ml"
    e.client_for = api_client_for
    return e
