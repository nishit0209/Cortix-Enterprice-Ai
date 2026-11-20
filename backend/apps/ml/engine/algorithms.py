"""Algorithm catalogue: estimators, default and allowed hyperparameters, bounded search spaces.

User-supplied hyperparameters are validated against an allowlist with ranges (no arbitrary keyword passthrough:
no `n_jobs`, callbacks, paths or objects reach an estimator).
"""

from __future__ import annotations

from typing import Any

ALGORITHMS: dict[str, tuple[str, ...]] = {
    "classification": ("logistic_regression", "random_forest", "xgboost", "hist_gradient_boosting"),
    "regression": ("ridge", "linear_regression", "random_forest", "xgboost", "hist_gradient_boosting"),
    "forecast": ("seasonal_naive", "ets", "sarimax"),
    "clustering": ("kmeans",),
}
ALGORITHM_LABELS = {
    "logistic_regression": "Logistic regression", "ridge": "Ridge regression",
    "linear_regression": "Linear regression", "random_forest": "Random forest", "xgboost": "XGBoost",
    "hist_gradient_boosting": "Histogram gradient boosting", "seasonal_naive": "Seasonal naive (baseline)",
    "ets": "Exponential smoothing (ETS)", "sarimax": "SARIMA", "kmeans": "K-Means",
}
LINEAR = {"logistic_regression", "ridge", "linear_regression"}
TREE = {"random_forest", "xgboost", "hist_gradient_boosting"}
SCALE_SENSITIVE = LINEAR | {"kmeans"}

# name -> (type, min, max) ; type "int" | "float" | "bool" | "choice:<a>|<b>" | "int_or_none"
PARAMS: dict[str, dict[str, tuple]] = {
    "logistic_regression": {"C": ("float", 1e-4, 1e4), "max_iter": ("int", 50, 10_000)},
    "ridge": {"alpha": ("float", 0.0, 1e5)},
    "linear_regression": {"fit_intercept": ("bool", None, None)},
    "random_forest": {"n_estimators": ("int", 10, 1000), "max_depth": ("int_or_none", 1, 64),
                      "min_samples_leaf": ("int", 1, 1000), "max_features": ("choice:sqrt|log2|1.0", None, None)},
    "xgboost": {"n_estimators": ("int", 10, 2000), "max_depth": ("int", 1, 16), "learning_rate": ("float", 1e-3, 1.0),
                "subsample": ("float", 0.1, 1.0), "colsample_bytree": ("float", 0.1, 1.0),
                "min_child_weight": ("float", 0.0, 100.0), "reg_lambda": ("float", 0.0, 1000.0)},
    "hist_gradient_boosting": {"max_iter": ("int", 10, 2000), "learning_rate": ("float", 1e-3, 1.0),
                               "max_depth": ("int_or_none", 1, 64), "max_leaf_nodes": ("int", 2, 1024),
                               "l2_regularization": ("float", 0.0, 1000.0)},
    "seasonal_naive": {},
    "ets": {"trend": ("bool", None, None), "damped": ("bool", None, None)},
    "sarimax": {"p": ("int", 0, 5), "d": ("int", 0, 2), "q": ("int", 0, 5)},
    "kmeans": {"n_clusters": ("int_or_none", 2, 30), "k_min": ("int", 2, 30), "k_max": ("int", 2, 30)},
}

SEARCH_SPACES: dict[str, dict[str, list]] = {
    "logistic_regression": {"C": [0.01, 0.1, 0.3, 1.0, 3.0, 10.0]},
    "ridge": {"alpha": [0.01, 0.1, 1.0, 10.0, 100.0]},
    "random_forest": {"n_estimators": [100, 200, 400], "max_depth": [None, 6, 10, 16],
                      "min_samples_leaf": [1, 2, 5, 10]},
    "xgboost": {"n_estimators": [150, 300, 500], "max_depth": [3, 4, 6, 8], "learning_rate": [0.03, 0.06, 0.1, 0.2],
                "subsample": [0.7, 0.85, 1.0], "colsample_bytree": [0.7, 0.85, 1.0]},
    "hist_gradient_boosting": {"max_iter": [100, 200, 400], "learning_rate": [0.03, 0.06, 0.1, 0.2],
                               "max_leaf_nodes": [15, 31, 63]},
}


class InvalidHyperparameters(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def algorithms_for(task: str) -> tuple[str, ...]:
    return ALGORITHMS[task]


def validate_params(algorithm: str, params: dict | None) -> dict:
    spec = PARAMS[algorithm]
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, value in (params or {}).items():
        if key not in spec:
            errors[key] = f"Unknown hyperparameter for {algorithm}. Allowed: {', '.join(sorted(spec)) or 'none'}."
            continue
        kind, lo, hi = spec[key]
        try:
            if kind == "bool":
                if not isinstance(value, bool):
                    raise TypeError
                out[key] = value
            elif kind.startswith("choice:"):
                choices = kind.split(":", 1)[1].split("|")
                if str(value) not in choices:
                    raise TypeError
                out[key] = float(value) if value in ("1.0", 1.0) else str(value)
            elif kind == "int_or_none" and value is None:
                out[key] = None
            else:
                if isinstance(value, bool):
                    raise TypeError
                num = int(value) if kind in ("int", "int_or_none") else float(value)
                if kind in ("int", "int_or_none") and float(value) != num:
                    raise TypeError
                if (lo is not None and num < lo) or (hi is not None and num > hi):
                    errors[key] = f"Must be between {lo} and {hi}."
                    continue
                out[key] = num
        except (TypeError, ValueError):
            errors[key] = f"Invalid value ({kind.split(':')[0]} expected)."
    if errors:
        raise InvalidHyperparameters(errors)
    return out


def default_params(algorithm: str, *, n_rows: int = 10_000) -> dict:
    if algorithm == "random_forest":
        return {"n_estimators": 200, "max_depth": 8 if n_rows < 1000 else None, "min_samples_leaf": 1}
    if algorithm == "xgboost":
        return {"n_estimators": 300, "max_depth": 4 if n_rows < 5000 else 6, "learning_rate": 0.06,
                "subsample": 0.9, "colsample_bytree": 0.9}
    if algorithm == "hist_gradient_boosting":
        return {"max_iter": 200, "learning_rate": 0.08}
    if algorithm == "logistic_regression":
        return {"C": 1.0, "max_iter": 2000}
    if algorithm == "ridge":
        return {"alpha": 1.0}
    return {}


def make_estimator(task: str, algorithm: str, params: dict, *, random_state: int, n_classes: int = 2,
                   balanced: bool = False, pos_weight: float | None = None):
    """A fresh unfitted estimator. `balanced` adds class weights (imbalanced classification)."""
    p = dict(params)
    if task == "classification":
        if algorithm == "logistic_regression":
            from sklearn.linear_model import LogisticRegression

            return LogisticRegression(C=p.get("C", 1.0), max_iter=p.get("max_iter", 2000),
                                      class_weight="balanced" if balanced else None, random_state=random_state)
        if algorithm == "random_forest":
            from sklearn.ensemble import RandomForestClassifier

            return RandomForestClassifier(n_estimators=p.get("n_estimators", 200), max_depth=p.get("max_depth"),
                                          min_samples_leaf=p.get("min_samples_leaf", 1),
                                          max_features=p.get("max_features", "sqrt"),
                                          class_weight="balanced" if balanced else None, n_jobs=-1,
                                          random_state=random_state)
        if algorithm == "xgboost":
            from xgboost import XGBClassifier

            extra = {"scale_pos_weight": pos_weight} if balanced and pos_weight and n_classes == 2 else {}
            return XGBClassifier(n_estimators=p.get("n_estimators", 300), max_depth=p.get("max_depth", 6),
                                 learning_rate=p.get("learning_rate", 0.06), subsample=p.get("subsample", 0.9),
                                 colsample_bytree=p.get("colsample_bytree", 0.9),
                                 min_child_weight=p.get("min_child_weight", 1.0), reg_lambda=p.get("reg_lambda", 1.0),
                                 tree_method="hist", random_state=random_state, n_jobs=0,
                                 eval_metric="logloss" if n_classes == 2 else "mlogloss", **extra)
        if algorithm == "hist_gradient_boosting":
            from sklearn.ensemble import HistGradientBoostingClassifier

            return HistGradientBoostingClassifier(max_iter=p.get("max_iter", 200),
                                                  learning_rate=p.get("learning_rate", 0.08),
                                                  max_depth=p.get("max_depth"),
                                                  max_leaf_nodes=p.get("max_leaf_nodes", 31),
                                                  l2_regularization=p.get("l2_regularization", 0.0),
                                                  class_weight="balanced" if balanced else None,
                                                  random_state=random_state)
    if task == "regression":
        if algorithm == "ridge":
            from sklearn.linear_model import Ridge

            return Ridge(alpha=p.get("alpha", 1.0), random_state=random_state)
        if algorithm == "linear_regression":
            from sklearn.linear_model import LinearRegression

            return LinearRegression(fit_intercept=p.get("fit_intercept", True))
        if algorithm == "random_forest":
            from sklearn.ensemble import RandomForestRegressor

            return RandomForestRegressor(n_estimators=p.get("n_estimators", 200), max_depth=p.get("max_depth"),
                                         min_samples_leaf=p.get("min_samples_leaf", 1),
                                         max_features=p.get("max_features", 1.0), n_jobs=-1,
                                         random_state=random_state)
        if algorithm == "xgboost":
            from xgboost import XGBRegressor

            return XGBRegressor(n_estimators=p.get("n_estimators", 300), max_depth=p.get("max_depth", 6),
                                learning_rate=p.get("learning_rate", 0.06), subsample=p.get("subsample", 0.9),
                                colsample_bytree=p.get("colsample_bytree", 0.9),
                                min_child_weight=p.get("min_child_weight", 1.0), reg_lambda=p.get("reg_lambda", 1.0),
                                tree_method="hist", random_state=random_state, n_jobs=0)
        if algorithm == "hist_gradient_boosting":
            from sklearn.ensemble import HistGradientBoostingRegressor

            return HistGradientBoostingRegressor(max_iter=p.get("max_iter", 200),
                                                 learning_rate=p.get("learning_rate", 0.08),
                                                 max_depth=p.get("max_depth"),
                                                 max_leaf_nodes=p.get("max_leaf_nodes", 31),
                                                 l2_regularization=p.get("l2_regularization", 0.0),
                                                 random_state=random_state)
    raise ValueError(f"Algorithm {algorithm!r} does not support task {task!r}.")
