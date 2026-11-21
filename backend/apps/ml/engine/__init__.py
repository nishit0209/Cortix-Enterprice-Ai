"""Pure ML engine of the ML workspace (no database access): column typing, leakage guard, EDA, recommendations,
preprocessing, training, metrics, explanations, artifacts, drift. Everything here runs on CPU with scikit-learn,
XGBoost, statsmodels and SHAP, and is unit-tested without a database (apps/ml/tests/test_engine_*.py).

The Django layer (apps.ml.services / tasks / views) loads policy-filtered frames, persists results and calls in.
"""
