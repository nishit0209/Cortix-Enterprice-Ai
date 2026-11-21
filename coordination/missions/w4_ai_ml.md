You own `backend/apps/{ai,nl2sql,documents,ml}/**`. THE FLAGSHIP OF THIS WAVE is the **ML WORKSPACE**, and it must REALLY work.

The user explicitly requires real model training and prediction on CPU, with no mocks. `docs/ML_ARCHITECTURE.md` (all sections) and `docs/API_SPECIFICATION.md` Phase 9 are binding, together with CONTRACT "W4 additions" (permissions, promote semantics, `predict_frame`, safety rules).

Installed: scikit-learn 1.9, xgboost 3.4, shap 0.54, statsmodels 0.15, joblib, polars, pandas, numpy, scipy. NOT installed: lightgbm, optuna. Do not use them, and do not pip install anything.

## A. `apps/ml` models

All are TenantModels with RLS and migrations:
- **MLProject:** name, description, dataset_id, target column, task_type (`classification|regression|forecast|clustering`, auto-detected but overridable), time column for forecasts, feature list, created_by.
- **DatasetSnapshot:** frozen dataset_version_id, the column list after policy, row count, sha256 of the training frame, sample info.
- **Experiment:** project, snapshot, algorithm, hyperparameters, `random_state`, status `queued|running|succeeded|failed`, metrics JSON (train/validation/test + CV mean/std), confusion matrix / residuals, feature importances, SHAP summary, duration, job_id, error.
- **MLModel** plus **ModelVersion:** experiment, artifact_key, artifact_sha256, feature schema with dtypes and categories, metrics, stage `none|staging|production|archived`, `promoted_by` / `promoted_at`.
- **PredictionLog:** model version, n_rows, source `api|ui|batch`, latency, caller (user or API key prefix), and a hash of the inputs (never raw rows).
- **DriftReport.**

## B. Workflow

Follow ML_ARCHITECTURE §1–§9, built REAL.

1. **Project creation.**
   - Target and feature selection is restricted by `effective_policy`: allowed, unmasked columns only. Return `excluded_by_policy`.
   - Leakage guard: drop ID-like columns (all unique, or names ending in `_id`/`id`) and columns that are near-duplicates of the target (|corr| > 0.98, or identical); list them.
2. **EDA** (202 job, local): per-column stats, missingness, target distribution / class balance, correlations (Pearson/Spearman), top categorical values, outliers, and time coverage for forecasts. The optional Gemini narration runs ONLY through `GeminiService` under the policy gate, with schema/aggregates only per AIDataPolicy; the default is a local template.
3. **Recommendations with reasons** (§5 rules: data size, task, class imbalance, feature types, interpretability). Rank 2–4 candidates with a plain-English reason each.
4. **Preprocessing.** A sklearn `Pipeline` + `ColumnTransformer`: impute (median / most_frequent), scale numeric for linear models, one-hot categorical with `handle_unknown='ignore'` (high-cardinality top-k plus other), datetime features (year, month, dow, etc.), and text columns excluded unless short categorical.
5. **Training.** A `TenantTask` on queue `ml` with job progress events.
   - Split: stratified train/test (time-ordered for forecasts), 5-fold CV on train.
   - Algorithms:
     - classification: LogisticRegression, RandomForest, XGBClassifier, HistGradientBoosting;
     - regression: Ridge/Linear, RandomForestRegressor, XGBRegressor, HistGradientBoostingRegressor;
     - forecast: statsmodels ExponentialSmoothing / SARIMAX with a seasonal-naive baseline;
     - clustering: KMeans with silhouette-based k selection.
   - Honour `ML_MAX_TRAIN_ROWS` / `ML_MAX_TRAIN_SECONDS`.
   - Metrics:
     - classification: accuracy, precision, recall, F1 (macro/weighted), ROC-AUC (binary/OvR), PR-AUC, log loss, confusion matrix;
     - regression: MAE, RMSE, R², MAPE (safe);
     - forecast: MAE, RMSE, MAPE vs baseline, horizon forecast with intervals;
     - clustering: silhouette, cluster sizes, centroids.
   - Feature importance (model-native or permutation), and a SHAP summary (TreeExplainer / LinearExplainer, bounded sample).
6. **Comparison** endpoint: side by side metrics; the best model is chosen by the primary metric per task.
7. **Registry.**
   - register: artifact = joblib dump of the FULL pipeline, written through `apps.core.storage` under `orgs/<org>/ml/models/<model>/<version>.joblib`, with its sha256 recorded.
   - promote per CONTRACT.
   - Loading verifies the sha256 and the org prefix, and keeps a small in-process LRU cache.
8. **Prediction.**
   - `POST …/predict`: JSON rows ≤ 1000, validated against the feature schema (missing column → 400 with a list; unknown categories are fine). Returns `prediction` (+ probabilities, + per-row SHAP top contributions when `explain=true`). Works with session auth (`ml.view`) AND API keys with scope `ml.predict`, using the existing ApiKeyAuthentication; keys are org-bound.
   - batch-predictions (202): over a dataset, written as a new dataset through `apps.datasets.services.publish_frame` (Data Engineering builds it IN PARALLEL with the exact CONTRACT signature; import lazily, and test with a monkeypatched fake).
   - `apps.ml.services.predict_frame` per CONTRACT.
9. **Monitoring.**
   - Drift: PSI per feature (numeric bins / categorical) between the training snapshot and recent prediction inputs (store binned reference stats on the version; keep compact per-feature histograms of prediction inputs, not raw rows), plus prediction distribution drift.
   - If labels arrive later through a dataset with the target, compute performance.
   - `GET …/monitoring`.
   - A periodic drift job registered via `apps.core.scheduler.register_periodic("ml.drift", 3600, fn)`.

Endpoints are exactly per API_SPECIFICATION Phase 9, with permissions per the CONTRACT. Isolation factories cover every resource route. Every mutation is audited.

## C. Wave-3 leftovers

1. **Documents.** Add `download_url` to DocumentDetailSerializer (signed via `apps.core.storage.presigned_get` after the `documents.view` check). Notify `document.processed` / `document.failed` via `apps.core.notifications.notify` (uploader, link `/documents/<id>`) if not already done.
2. **Quota-notification unit tests.** `apps/ai/tests/test_w3_quota.py::test_first_org_exhaustion_of_the_day_notifies_admins_once` and `::test_notify_falls_back_to_member_lookup_and_never_raises` fail on main now that the real `apps.core.notifications` is merged: they reach the DB. Stub `members_with_permission` / `notify_permission`, or mark them as DB tests correctly.
3. **Insights forwarding.** You asked the Backend to forward `sql` / `truncated` to `compute_insights`. The Backend does it this wave; keep your function signature stable.

## D. Tests

These need no DB:
- every algorithm on small synthetic frames (deterministic seeds, metric sanity versus known data);
- preprocessing (unknown categories, missing values);
- leakage guard;
- recommendation rules;
- artifact sha256 verification (a tampered artifact is refused);
- predict validation;
- PSI math against hand-computed values;
- forecast with a seasonal series;
- clustering k selection.

Write DB/API tests for: projects, experiments, registry, predict with session AND API key, cross-org 404, and the policy-excluded columns.

Report REAL counts. Use the testdata CSVs (`testdata/generated/customers.csv`, `orders.csv`, etc.) for an end-to-end, non-DB training test of churn classification and revenue regression, and report the metrics you get.
