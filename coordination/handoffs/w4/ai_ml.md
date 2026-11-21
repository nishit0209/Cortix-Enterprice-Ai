# W4 handoff: ai_ml

## Done
- **ML engine** (CPU, real): typing, leakage guard, EDA, recommendations with reasons, sklearn preprocessing pipeline, training (LR/RF/XGB/HGB, Ridge/Linear, ETS/SARIMAX/seasonal-naive, KMeans with silhouette k), metrics, SHAP/permutation importance, PSI drift, input validation.
- **apps.ml**: models + RLS, governed data access (effective_policy, `excluded_by_policy`), sha256-verified artifacts under `orgs/<org>/ml/...`, snapshots, training TenantTask (queue `ml`), compare, register, promote (one production version per model), `predict_rows`, `predict_frame` + `ModelNotReady` (re-exported from `apps.ml.services`), batch predictions via `apps.datasets.services.publish_frame` (lazy import), monitoring, periodic `ml.drift` job.
- **Phase 9 API** (`apps/ml/views.py`, `urls.py`): projects (list/create/detail/PATCH/DELETE=archive), `eda` GET/POST, `recommendations`, `snapshots` GET/POST, `experiments` GET/POST, `experiments/{id}`, `experiments/compare?ids=`, `register`, `models` list/detail/`versions`, `versions/{v}/promote` (ml.promote), `predict` (ml.view session OR API key with ml.predict), `batch-predictions` (202), `monitoring`. Lists use `{items}`. Isolation factories: `apps/ml/tests/isolation.py`.
- Fixes: `/ai/usage` 500 (date passed as datetime); documents xlsx processing failed (openpyxl refuses suffix-less mkstemp paths); ML batch task dropped `classification`/`attest`; API-key prefix missing in PredictionLog; documents PATCH/classification and ML POST routes now 404 for cross-org ids before validating the body.

## Tests (real DB, PYTEST_DB_SUFFIX=ai_ml)
- apps/ai + apps/nl2sql + apps/documents + apps/ml: **954 passed, 0 failed**.
- tests/isolation (all apps), earlier run: 2227 passed, 11 failed. 3 of those were mine and are fixed (ml promote route discovery, documents PATCH and classification). The 8 others are in other owners' code, listed below.
- E2E metrics on testdata: churn logistic regression ROC-AUC 0.826, accuracy 0.815 (oracle AUC 0.813); revenue XGBoost R2 0.814, RMSE 2536.85, MAE 174.68 (heavy-tailed revenue).

## Requests
- Backend: the Analyst role has only `ml.view`. Per CONTRACT it should also have `ml.manage`.
- Frontend: lists return `{items, next_cursor}`. Register returns `{model_id, version, model, model_version}`. Predict accepts `X-Nexora-Client: ui` to log the source as `ui`. `POST ml/projects/preview` is not implemented, so use the wizard's local fallback.

## Defects for others
- datasets: `POST /orgs/{org}/uploads` answers 415 before tenant resolution for a non-member (isolation matrix expects 404), and fails the CSRF matrix case.
- bi: `POST bi/messages/{id}/feedback` validates the body before resolving the object, so a cross-org id gets 400 instead of 404.
- core: `tests/isolation/test_jobs_sse_isolation.py` (2 tests): `_check_access` returns None for a viewer's own-org job.
