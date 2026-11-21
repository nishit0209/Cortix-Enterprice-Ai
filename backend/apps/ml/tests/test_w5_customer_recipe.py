"""Customer 360 ML recipe: point-in-time features (no leakage), and the churn + CLV projects trained on synthetic
customers + orders ingested through the real upload path."""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl
import pytest

from apps.datasets.tests.helpers import csv_bytes, ingest
from apps.ml import recipes, services


def _synthetic(n_customers: int = 150, seed: int = 11):
    rnd = random.Random(seed)
    start = date(2025, 1, 1)
    customers = [["customer_id", "customer_name", "email", "region", "segment", "loyalty_tier", "signup_date",
                  "date_of_birth", "churned"]]
    orders = [["order_id", "order_date", "customer_id", "product_id", "quantity", "unit_price", "discount", "amount",
               "channel", "status"]]
    oid = 0
    for i in range(n_customers):
        cid = f"C{i:05d}"
        churned = int(rnd.random() < 0.3)
        tier = rnd.choice(["Bronze", "Silver", "Gold"])
        customers.append([cid, f"Synthetic Person {i}", f"person{i}@example.test", rnd.choice(["North", "South"]),
                          rnd.choice(["Consumer", "SMB"]), tier, (start - timedelta(days=rnd.randint(30, 900))).isoformat(),
                          date(rnd.randint(1960, 2004), rnd.randint(1, 12), rnd.randint(1, 28)).isoformat(), churned])
        rate = 3 if tier == "Gold" else (2 if tier == "Silver" else 1)
        active_until = 300 if churned else 540
        for _ in range(rate * rnd.randint(2, 6)):
            oid += 1
            day = rnd.randint(0, active_until)
            qty = rnd.randint(1, 4)
            price = round(rnd.uniform(100, 900) * (1.5 if tier == "Gold" else 1.0), 2)
            orders.append([f"T{oid:07d}", (start + timedelta(days=day)).isoformat(), cid, f"P{rnd.randint(1, 30):03d}",
                           qty, price, 0.0, round(qty * price, 2), rnd.choice(["Online", "Store"]),
                           "returned" if rnd.random() < 0.05 else "completed"])
    return customers, orders


def _frame(rows):
    return pl.DataFrame(rows[1:], schema=rows[0], orient="row")


def test_customer_features_are_point_in_time_and_drop_pii():
    customers, orders = _synthetic()
    frame, as_of = recipes.customer_features(_frame(customers), _frame(orders), horizon_days=180)
    assert frame.height == 150 and frame["customer_id"].n_unique() == 150
    for col in ("tenure_days", "age", "orders_count", "total_spend", "days_since_last_order", "return_rate",
                "distinct_products", "online_share", "churned", "clv_next_180d"):
        assert col in frame.columns, col
    assert not {"customer_name", "email", "signup_date", "date_of_birth"} & set(frame.columns)
    # no leakage: behaviour only counts orders on/before as_of, the target only orders after it
    o = _frame(orders).with_columns(pl.col("order_date").str.strptime(pl.Date), pl.col("amount").cast(pl.Float64))
    assert as_of == o["order_date"].max() - timedelta(days=180)
    cid = frame.filter(pl.col("orders_count") > 0)["customer_id"][0]
    past = o.filter((pl.col("customer_id") == cid) & (pl.col("order_date") <= as_of))
    future = o.filter((pl.col("customer_id") == cid) & (pl.col("order_date") > as_of)
                      & (pl.col("status") == "completed"))
    row = frame.filter(pl.col("customer_id") == cid).row(0, named=True)
    assert row["orders_count"] == past.height
    assert row["clv_next_180d"] == pytest.approx(round(future["amount"].sum(), 2), abs=0.02)
    assert frame.filter(pl.col("churned") == 1)["clv_next_180d"].mean() < \
        frame.filter(pl.col("churned") == 0)["clv_next_180d"].mean()


def test_customer_features_reject_short_history():
    customers, orders = _synthetic(20)
    with pytest.raises(Exception, match="shorter than the CLV horizon"):
        recipes.customer_features(_frame(customers), _frame(orders), horizon_days=5000)


@pytest.mark.django_db(transaction=False)
def test_recipe_creates_and_trains_churn_and_clv(two_orgs, api_client_for, memory_storage,
                                                 django_capture_on_commit_callbacks, settings):
    settings.ML_PRODUCTION_REQUIRES_APPROVAL = False
    a = two_orgs.a
    owner = api_client_for(a.owner)
    customers, orders = _synthetic()
    _r, cds = ingest(owner, a.org, "customers.csv", csv_bytes(customers), django_capture_on_commit_callbacks,
                     name="Customers (synthetic)")
    _r, ods = ingest(owner, a.org, "orders.csv", csv_bytes(orders), django_capture_on_commit_callbacks,
                     name="Orders (synthetic)")
    assert cds["status"] == "ready" and ods["status"] == "ready", (cds, ods)
    with django_capture_on_commit_callbacks(execute=True), a.tenant():
        published = services.publish_customer_features(user_id=a.owner.pk, customers_dataset_id=cds["id"],
                                                       orders_dataset_id=ods["id"])
    assert published["rows"] == 150
    from apps.datasets.models import DatasetVersion

    with a.tenant():
        assert DatasetVersion.objects.get(id=published["version_id"]).status == "ready"
    out = services.train_customer_projects(org_id=a.org.id, user_id=a.owner.pk,
                                           features_dataset_id=published["dataset_id"], promote_to="production")
    assert set(out["projects"]) == {"churn", "clv"} and all(out["projects"].values())
    from apps.ml.models import MLProject

    with a.tenant():
        churn = MLProject.objects.get(id=out["projects"]["churn"])
        clv = MLProject.objects.get(id=out["projects"]["clv"])
    assert churn.task_type == "classification" and churn.target == "churned"
    assert clv.task_type == "regression" and clv.target == "clv_next_180d"
    for p in (churn, clv):
        assert not {"customer_id", "customer_name", "email"} & set(p.features)
    assert "clv_next_180d" not in churn.features and "churned" not in clv.features
    for key in ("churn", "clv"):
        model = out["models"][key]
        assert model["stage"] == "production" and model["metrics"]["primary_value"] is not None, model
    # C360 scoring path: predict_frame on the feature rows
    with a.tenant():
        from apps.ml.data import governed_frame

        frame = governed_frame(a.owner.pk, published["dataset_id"]).frame.head(10)
        scored = services.predict_frame(out["models"]["churn"]["model_id"], frame)
    assert scored.height == 10 and {"prediction", "probability"} <= set(scored.columns)
    # idempotent: running the project step again reuses the projects
    again = services.train_customer_projects(org_id=a.org.id, user_id=a.owner.pk,
                                             features_dataset_id=published["dataset_id"], train=False)
    assert again["projects"] == out["projects"]
