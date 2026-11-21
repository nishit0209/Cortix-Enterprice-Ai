"""Customer 360 ML recipe: churn (classification) and CLV (regression) projects from customers + orders.

    customer_features(customers, orders, *, horizon_days=180) -> polars.DataFrame     pure, no database
    publish_customer_features(user_id=, customers_dataset_id=, orders_dataset_id=, ...) -> DatasetVersion
    create_customer_projects(user_id=, features_dataset_id=, ...) -> {"churn": MLProject, "clv": MLProject}
    customer360_recipe(user_id=, customers_dataset_id=, orders_dataset_id=, train=False, ...) -> dict

Point-in-time design (no leakage): `as_of` = the last order date minus `horizon_days`. Every behavioural feature
uses orders ON OR BEFORE `as_of`; the CLV target `clv_next_<h>d` is the completed spend AFTER `as_of`. The churn
target is the customers dataset's own `churned` label. Identifiers, names and contact columns (PII) never become
features, and both source frames are read through the user's data policy (masked/denied columns are skipped and
reported as `excluded_by_policy`).

Used by `manage.py ml_customer_recipe` and by the demo seed, so C360 scoring (`customers.score`, which calls
`apps.ml.services.predict_frame`) has a production churn and CLV model to use.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import polars as pl
from rest_framework.exceptions import ValidationError

log = logging.getLogger("nexora.ml")

DEFAULT_HORIZON_DAYS = 180
CHURN_PROJECT = "Customer churn"
CLV_PROJECT = "Customer lifetime value"
FEATURES_DATASET = "Customer ML features"
# Never features: identifiers, names and contact details (and the raw dates the features are derived from).
NON_FEATURES = ("customer_id", "customer_name", "name", "email", "phone", "date_of_birth", "signup_date",
                "first_name", "last_name", "address", "pincode")
CATEGORICAL = ("city", "region", "segment", "loyalty_tier", "marketing_opt_in", "gender")
COMPLETED = ("completed", "delivered", "paid", "fulfilled", "shipped")
RETURNED = ("returned", "refunded", "cancelled", "canceled")


def _as_date(col: str, frame: pl.DataFrame) -> pl.Expr:
    dtype = frame.schema.get(col)
    if dtype == pl.Date:
        return pl.col(col)
    if isinstance(dtype, pl.Datetime):
        return pl.col(col).dt.date()
    return pl.col(col).cast(pl.Utf8).str.slice(0, 10).str.strptime(pl.Date, "%Y-%m-%d", strict=False)


def _num(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Float64, strict=False)


def clv_target_name(horizon_days: int) -> str:
    return f"clv_next_{int(horizon_days)}d"


def customer_features(customers: pl.DataFrame, orders: pl.DataFrame, *, horizon_days: int = DEFAULT_HORIZON_DAYS,
                      as_of: date | None = None) -> tuple[pl.DataFrame, date]:
    """One row per customer: profile + behaviour before `as_of`, `churned` and the CLV target after it."""
    for name, frame, need in (("customers", customers, ("customer_id",)),
                              ("orders", orders, ("customer_id", "order_date", "amount"))):
        missing = [c for c in need if c not in frame.columns]
        if missing:
            raise ValidationError({name: [f"Missing column(s): {', '.join(missing)}."]})
    o = orders.with_columns(
        _as_date("order_date", orders).alias("_d"), _num("amount").fill_null(0.0).alias("_amt"),
        pl.col("customer_id").cast(pl.Utf8),
        (pl.col("status").cast(pl.Utf8).str.to_lowercase() if "status" in orders.columns
         else pl.lit("completed")).alias("_status"),
        (_num("quantity") if "quantity" in orders.columns else pl.lit(1.0)).alias("_qty"),
        (_num("discount") if "discount" in orders.columns else pl.lit(0.0)).alias("_disc"),
        (pl.col("channel").cast(pl.Utf8).str.to_lowercase() if "channel" in orders.columns
         else pl.lit("")).alias("_channel"),
        (pl.col("product_id").cast(pl.Utf8) if "product_id" in orders.columns else pl.lit(None)).alias("_pid"),
    ).filter(pl.col("_d").is_not_null())
    if o.height == 0:
        raise ValidationError({"orders": ["No order has a readable order_date."]})
    last = o["_d"].max()
    as_of = as_of or (last - timedelta(days=int(horizon_days)))
    first = o["_d"].min()
    if as_of <= first:
        raise ValidationError({"horizon_days": ["The order history is shorter than the CLV horizon."]})
    kept = ~pl.col("_status").is_in(list(RETURNED))
    past = o.filter(pl.col("_d") <= as_of)
    future = o.filter((pl.col("_d") > as_of) & kept)
    behaviour = past.group_by("customer_id").agg(
        pl.len().alias("orders_count"),
        pl.col("_amt").filter(kept).sum().alias("total_spend"),
        pl.col("_amt").filter(kept).mean().alias("avg_order_value"),
        pl.col("_amt").filter(kept & (pl.col("_d") > as_of - timedelta(days=90))).sum().alias("spend_last_90d"),
        pl.col("_d").max().alias("_last"), pl.col("_d").min().alias("_first"),
        pl.col("_status").is_in(list(RETURNED)).mean().alias("return_rate"),
        pl.col("_disc").mean().alias("avg_discount"),
        pl.col("_qty").sum().alias("units"),
        pl.col("_pid").n_unique().alias("distinct_products"),
        (pl.col("_channel") == "online").mean().alias("online_share"),
    ).with_columns(
        (pl.lit(as_of) - pl.col("_last")).dt.total_days().alias("days_since_last_order"),
        (pl.lit(as_of) - pl.col("_first")).dt.total_days().alias("days_as_buyer"),
    ).drop("_last", "_first")
    clv = clv_target_name(horizon_days)
    targets = future.group_by("customer_id").agg(pl.col("_amt").sum().alias(clv))

    c = customers.with_columns(pl.col("customer_id").cast(pl.Utf8))
    profile = [pl.col("customer_id")]
    if "signup_date" in c.columns:
        profile.append((pl.lit(as_of) - _as_date("signup_date", c)).dt.total_days().alias("tenure_days"))
    if "date_of_birth" in c.columns:
        profile.append(((pl.lit(as_of) - _as_date("date_of_birth", c)).dt.total_days() / 365.25).floor()
                       .alias("age"))
    profile += [pl.col(col).cast(pl.Utf8) for col in CATEGORICAL if col in c.columns]
    if "churned" in c.columns:
        profile.append(_num("churned").round(0).cast(pl.Int64, strict=False).alias("churned"))
    out = (c.select(profile)
           .join(behaviour, on="customer_id", how="left")
           .join(targets, on="customer_id", how="left")
           .with_columns(pl.col(col).fill_null(0) for col in (
               "orders_count", "total_spend", "spend_last_90d", "units", "distinct_products", clv))
           .with_columns(pl.col("days_since_last_order").fill_null(pl.col("tenure_days") if "tenure_days" in
                                                                   c.columns else 9999))
           .with_columns(pl.col(pl.Float64).round(2)))
    if "tenure_days" in out.columns:  # customers who signed up after as_of did not exist yet at as_of
        out = out.filter(pl.col("tenure_days").is_null() | (pl.col("tenure_days") >= 0))
    return out.sort("customer_id"), as_of


def _governed(user_id, dataset_id):
    from .data import governed_frame

    return governed_frame(user_id, dataset_id)


def publish_customer_features(*, user_id, customers_dataset_id, orders_dataset_id,
                              horizon_days: int = DEFAULT_HORIZON_DAYS, name: str = FEATURES_DATASET,
                              attest: bool = False, dataset_id=None) -> dict:
    """Build the feature table from the user's policy view of both datasets and publish it (publish_frame).

    The output is classified like the most sensitive source; synthetic/public outputs need `attest=True` (else
    `internal`)."""
    from apps.datasets.services import publish_frame

    cust, orders = _governed(user_id, customers_dataset_id), _governed(user_id, orders_dataset_id)
    frame, as_of = customer_features(cust.frame, orders.frame, horizon_days=horizon_days)
    order = ["synthetic", "public", "internal", "confidential"]
    worst = max((cust.classification, orders.classification), key=lambda k: order.index(k) if k in order else 3)
    classification = worst if (worst not in ("synthetic", "public") or attest) else "internal"
    version = publish_frame(
        name=name, frame=frame, user_id=user_id, classification=classification, attest=attest,
        dataset_id=dataset_id, source="ml",
        description=(f"Customer features as of {as_of.isoformat()} from '{cust.dataset_name}' and "
                     f"'{orders.dataset_name}'; target {clv_target_name(horizon_days)} = completed spend in the "
                     f"next {horizon_days} days. Built by the Customer 360 ML recipe."))
    return {"dataset_id": str(version.dataset_id), "version_id": str(version.id), "version": version.version,
            "status": version.status, "rows": frame.height, "as_of": as_of.isoformat(),
            "excluded_by_policy": {"customers": cust.excluded_by_policy, "orders": orders.excluded_by_policy}}


def create_customer_projects(*, user_id, features_dataset_id, horizon_days: int = DEFAULT_HORIZON_DAYS,
                             request=None) -> dict:
    """The churn (classification, `churned`) and CLV (regression, `clv_next_<h>d`) projects on the features."""
    from . import services
    from .models import MLProject

    clv = clv_target_name(horizon_days)
    gf = _governed(user_id, features_dataset_id)
    out = {}
    for key, name, target, task, drop in (("churn", CHURN_PROJECT, "churned", "classification", clv),
                                          ("clv", CLV_PROJECT, clv, "regression", "churned")):
        if target not in gf.allowed_columns:
            out[key] = None
            continue
        existing = MLProject.objects.filter(name=name, dataset_id=gf.dataset_id, status="active").first()
        if existing is not None:
            out[key] = existing
            continue
        features = [c for c in gf.allowed_columns if c not in (target, drop, *NON_FEATURES)]
        out[key] = services.create_project(user_id=user_id, request=request, data={
            "name": name, "dataset_id": str(gf.dataset_id), "target": target, "task_type": task,
            "features": features,
            "description": f"Customer 360 recipe: {task} on {target}."})
    return out


def customer360_recipe(*, org_id, user_id, customers_dataset_id, orders_dataset_id,
                       horizon_days: int = DEFAULT_HORIZON_DAYS, train: bool = False, promote_to: str = "staging",
                       attest: bool = False, wait_s: float = 900.0) -> dict:
    """Features -> projects -> (optionally) train, register and promote the recommended algorithm of each.

    Opens its own tenant transactions (the feature dataset is ingested after the publishing transaction commits),
    so call it OUTSIDE a tenant context: from `manage.py ml_customer_recipe`, the demo seed or a worker. Training
    runs inline."""
    import time

    from apps.core.tenancy import tenant_context
    from apps.datasets.models import DatasetVersion


    with tenant_context(org_id):
        published = publish_customer_features(user_id=user_id, customers_dataset_id=customers_dataset_id,
                                              orders_dataset_id=orders_dataset_id, horizon_days=horizon_days,
                                              attest=attest)
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while True:  # eager runner: already ingested on commit; a Celery worker: wait for the ingest job
        with tenant_context(org_id):
            status = DatasetVersion.objects.filter(id=published["version_id"]).values_list("status", flat=True)
            published["status"] = status.first() or "missing"
        if published["status"] in ("ready", "failed", "missing") or time.monotonic() >= deadline:
            break
        time.sleep(2.0)
    result: dict = {"features": published, "projects": {}, "models": {}}
    if published["status"] != "ready":
        result["message"] = (f"The feature dataset is {published['status']}; once it is ready run "
                             f"`manage.py ml_customer_recipe --features {published['dataset_id']}`.")
        return result
    result.update(train_customer_projects(org_id=org_id, user_id=user_id, features_dataset_id=published["dataset_id"],
                                          horizon_days=horizon_days, train=train, promote_to=promote_to))
    return result


def train_customer_projects(*, org_id, user_id, features_dataset_id, horizon_days: int = DEFAULT_HORIZON_DAYS,
                            train: bool = True, promote_to: str = "staging") -> dict:
    """Projects on an existing feature dataset; with `train`, the top recommendation of each is trained,
    registered and promoted (`production` goes through an approval)."""
    from apps.core.tenancy import tenant_context

    from . import services

    out: dict = {"projects": {}, "models": {}}
    with tenant_context(org_id):
        projects = create_customer_projects(user_id=user_id, features_dataset_id=features_dataset_id,
                                            horizon_days=horizon_days)
        out["projects"] = {k: (str(p.id) if p else None) for k, p in projects.items()}
    if not train:
        return out
    for key, project in projects.items():
        if project is None:
            continue
        with tenant_context(org_id):
            best = services.recommendations(project)["candidates"][:1]
            _c, _job, _snap, run = services.start_experiments(
                project, user_id=user_id, data={"algorithms": [b["algorithm"] for b in best]}, inline=True)
            best_id = (run or {}).get("best_experiment_id")
            if not best_id:
                out["models"][key] = {"status": "failed", "experiments": (run or {}).get("experiments", [])}
                continue
            version = services.register(services.get_experiment(best_id), user_id=user_id,
                                        data={"model_name": project.name})
            entry = {"model_id": str(version.model_id), "version": version.version, "algorithm": version.algorithm,
                     "metrics": {k: version.metrics.get(k) for k in ("primary_metric", "primary_value")}}
            if promote_to in ("staging", "production"):
                outcome, obj = services.request_promotion(version.model, version.version, promote_to,
                                                          user_id=user_id)
                entry["stage"] = obj.stage if outcome == "promoted" else "pending_approval"
                if outcome == "pending":
                    entry["approval_id"] = str(obj.id)
            out["models"][key] = entry
    return out
