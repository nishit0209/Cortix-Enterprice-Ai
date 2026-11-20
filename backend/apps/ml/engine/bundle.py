"""The model artifact: a `ModelBundle` (full sklearn Pipeline incl. preprocessing, or a fitted forecaster) plus
the metadata prediction needs. Serialized with joblib; integrity is a sha256 over the exact bytes.

joblib/pickle is unsafe on untrusted input, so ONLY bytes whose sha256 matches the value Nexora recorded when it
trained the model are ever deserialized (`loads_verified`). User-uploaded model files are not supported.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


class ArtifactIntegrityError(Exception):
    """The artifact bytes do not match the recorded sha256 (tampered or corrupted): never deserialized."""


def lib_versions() -> dict[str, str]:
    import joblib
    import sklearn
    import xgboost

    import statsmodels

    return {"sklearn": sklearn.__version__, "xgboost": xgboost.__version__, "numpy": np.__version__,
            "pandas": pd.__version__, "statsmodels": statsmodels.__version__, "joblib": joblib.__version__}


@dataclass
class ModelBundle:
    task: str
    algorithm: str
    target: str | None
    feature_schema: list[dict]
    pipeline: Any = None                 # sklearn Pipeline (classification / regression / clustering)
    forecast: Any = None                 # engine.forecast.ForecastModel
    classes: list | None = None          # original class labels, index = encoded label
    encoded_means: list | None = None    # training means of the encoded matrix (linear SHAP masker)
    encoded_std: list | None = None
    random_state: int = 42
    time_column: str | None = None
    versions: dict = field(default_factory=dict)

    @property
    def feature_names(self) -> list[str]:
        return [c["name"] for c in self.feature_schema]

    def predict(self, X: pd.DataFrame) -> dict:
        """{"prediction": array (original labels / values / cluster ids), "proba": (n, C) or None,
        "class_index": array or None}."""
        if self.task == "forecast":
            raise ValueError("Forecast models predict by horizon or timestamps, not feature rows.")
        pipe = self.pipeline
        if self.task == "classification":
            proba = pipe.predict_proba(X) if hasattr(pipe, "predict_proba") else None
            idx = proba.argmax(axis=1) if proba is not None else np.asarray(pipe.predict(X), dtype=int)
            labels = np.asarray(self.classes, dtype=object)[idx]
            return {"prediction": labels, "proba": proba, "class_index": idx}
        if self.task == "clustering":
            return {"prediction": np.asarray(pipe.predict(X), dtype=int), "proba": None, "class_index": None}
        return {"prediction": np.asarray(pipe.predict(X), dtype=float), "proba": None, "class_index": None}


def dumps(bundle: ModelBundle) -> bytes:
    import joblib

    buf = io.BytesIO()
    joblib.dump(bundle, buf, compress=3)
    return buf.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def loads_verified(data: bytes, expected_sha256: str) -> ModelBundle:
    import joblib

    if not expected_sha256 or sha256(data) != expected_sha256:
        raise ArtifactIntegrityError("Model artifact checksum mismatch: refusing to load it.")
    bundle = joblib.load(io.BytesIO(data))
    if not isinstance(bundle, ModelBundle):
        raise ArtifactIntegrityError("The artifact is not a Nexora model bundle.")
    return bundle
