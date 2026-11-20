"""Model recommendation with reasons (ML_ARCHITECTURE §5): rules over the data profile and the objective.

`recommend(...)` returns 2-4 ranked candidates, each with plain-English reasons, plus warnings and the suggested
primary metric. It never trains anything; the user may pick another algorithm (the override is recorded).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .algorithms import ALGORITHM_LABELS, algorithms_for

EXPLAIN_RE = re.compile(r"complian|credit|loan|regulat|explain|interpret|audit|fair|transparen|lending|insur",
                        re.I)

SIMPLICITY = {  # lower = simpler (tie-break)
    "logistic_regression": 0, "ridge": 0, "linear_regression": 0, "seasonal_naive": 0, "kmeans": 0,
    "ets": 1, "random_forest": 1, "sarimax": 2, "hist_gradient_boosting": 2, "xgboost": 3,
}


def primary_metric_for(task: str, *, n_classes: int | None = None, minority_share: float | None = None) -> str:
    if task == "classification":
        if minority_share is not None and minority_share < 0.10:
            return "pr_auc"
        return "roc_auc" if (n_classes or 2) == 2 else "f1_macro"
    if task == "regression":
        return "rmse"
    if task == "forecast":
        return "mae"
    return "silhouette"


def recommend(*, task: str, n_rows: int, kinds: Mapping[str, str], features: Sequence[str],
              business_goal: str = "", n_classes: int | None = None, minority_share: float | None = None,
              encoded_width: int | None = None, periods: int | None = None, season_length: int | None = None
              ) -> dict:
    feats = list(features)
    n_cat = sum(1 for f in feats if kinds.get(f) == "categorical")
    n_num = sum(1 for f in feats if kinds.get(f) in ("numeric", "boolean", "datetime"))
    width = encoded_width or (n_num + 10 * n_cat)
    candidates = algorithms_for(task)
    score = {a: 0.0 for a in candidates}
    reasons: dict[str, list[str]] = {a: [] for a in candidates}
    warnings: list[str] = []
    explain = bool(EXPLAIN_RE.search(business_goal or ""))

    def add(algo: str, pts: float, why: str) -> None:
        if algo in score:
            score[algo] += pts
            reasons[algo].append(why)

    if task in ("classification", "regression"):
        linear = "logistic_regression" if task == "classification" else "ridge"
        for a, base in (("random_forest", 3), ("xgboost", 3), ("hist_gradient_boosting", 2), (linear, 2),
                        ("linear_regression", 1)):
            if a in score:
                score[a] += base
        add("random_forest", 0, "Robust default for tabular data: handles non-linear effects and mixed feature "
                                "types with little tuning.")
        add("xgboost", 0, "Gradient boosting usually gives the best accuracy on tabular data.")
        add(linear, 0, "Simple, fast and directly interpretable through its coefficients.")
        add("hist_gradient_boosting", 0, "Fast gradient boosting that handles missing values natively.")
        if n_rows < 1000:
            add(linear, 2, f"Only {n_rows} rows: a low-variance linear model is less likely to overfit.")
            add("random_forest", 1, "With few rows, a shallow random forest stays stable.")
            add("xgboost", -1, "Boosting can overfit on fewer than 1,000 rows.")
            add("hist_gradient_boosting", -2, "Histogram boosting needs more rows to shine.")
            warnings.append(f"Only {n_rows} rows: metrics will have wide uncertainty.")
        if n_rows > 5000 and (n_cat and n_num):
            add("xgboost", 2, "More than 5,000 rows with mixed numeric and categorical features favour boosting.")
            add("random_forest", 1, "Enough rows to capture non-linear interactions.")
            add("hist_gradient_boosting", 2, "Scales well beyond 5,000 rows.")
        if n_rows > 50_000:
            add("hist_gradient_boosting", 1, "Histogram binning keeps training fast on large data.")
            add("random_forest", -1, "Random forests get slow and large beyond 50,000 rows.")
        if explain:
            add(linear, 3, "Your goal mentions explainability/compliance: coefficients are the clearest "
                           "explanation; tree models follow with SHAP.")
        if width > 200:
            add(linear, 1, "Many encoded features: a regularised linear model handles high dimensionality well.")
        if task == "classification" and minority_share is not None and minority_share < 0.10:
            add("xgboost", 1, "Strong class imbalance: class-weighted, calibrated boosting copes best; judge by "
                              "PR-AUC and F1, not accuracy.")
            add("random_forest", 0.5, "Class weights counter the imbalance.")
            warnings.append("Minority class below 10%: accuracy is misleading; the primary metric is PR-AUC.")
        if task == "classification" and n_classes and n_classes > 10:
            warnings.append(f"{n_classes} classes: consider grouping rare classes.")
    elif task == "forecast":
        score.update({"seasonal_naive": 1.0, "ets": 3.0, "sarimax": 2.0})
        add("seasonal_naive", 0, "Baseline: repeats the last season. Every model must beat it.")
        add("ets", 0, "Exponential smoothing captures level, trend and seasonality with few parameters.")
        add("sarimax", 0, "SARIMA models autocorrelation explicitly; best with long histories.")
        if periods is not None and periods >= 100:
            add("sarimax", 1.5, f"{periods} periods of history are enough to estimate SARIMA terms reliably.")
        if periods is not None and periods < 30:
            add("sarimax", -1, f"Only {periods} periods: SARIMA estimates will be unstable.")
            warnings.append(f"Only {periods} periods of history: forecasts will be uncertain.")
        if season_length and season_length > 1:
            add("ets", 0.5, f"A seasonal cycle of {season_length} periods was detected.")
        warnings.append("Time-based split: the most recent periods are held out (never a random split).")
    else:  # clustering
        score["kmeans"] = 1.0
        add("kmeans", 0, "K-Means with a silhouette sweep over k = 2..10 picks the number of groups for you.")
        if n_rows > 100_000:
            warnings.append("Large data: clustering runs on a sample for the k sweep.")

    ranked = sorted(score, key=lambda a: (-score[a], SIMPLICITY.get(a, 9)))[:4]
    items = [{"algorithm": a, "label": ALGORITHM_LABELS[a], "rank": i + 1, "score": round(score[a], 2),
              "reasons": reasons[a]} for i, a in enumerate(ranked)]
    return {"task": task, "candidates": items,
            "primary_metric": primary_metric_for(task, n_classes=n_classes, minority_share=minority_share),
            "warnings": warnings, "explainability_required": explain}
