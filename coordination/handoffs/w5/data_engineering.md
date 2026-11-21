# W5 handoff: data_engineering

## Done
- **Automation steps** (CONTRACT W5): `pipelines.run` {pipeline_id}, `customers.resolve` {}, `customers.score` {score_type, model_id?}, `products.normalize` {}.
  - Registered in each `AppConfig.ready()` through `apps.datasets.automation.register_steps`. It imports `apps.automation.registry` lazily and is a no-op when that is missing.
  - Every handler re-checks the actor's permission with `members_with_permission`. The actor comes from `ctx.actor_id`, `user_id`, `actor` or `user`, or `ctx.run.actor_id`/`created_by`/`requested_by`/`triggered_by`, else the current audit actor.
  - Each handler starts the same 202 job as the API and returns `{run_id, job_id, ...}`.
  - Pipeline runs started this way get `trigger="automation"` (migration `pipelines/0003`).
- **Events** via `apps.automation.events.emit(kind, payload)`. The import is lazy, the emit runs inside the org's tenant context, and it never raises.
  - `dataset.ingest_finished`: receiver of `dataset_ingest_finished`. Payload: dataset_id, job_id, ok, status, code, dataset_name, classification, version_id, version, row_count, quality_score, current, org_id.
  - `pipeline.run_failed`: receiver of `pipeline_run_failed`.
  - `pipeline.run_succeeded`: sent on commit from `runner._succeed`.
  - Run payload: run_id, pipeline_id, pipeline_name, number, status, trigger, attempt, rows_out, error_code, output_dataset_id, output_dataset_version_id, org_id.
- **C360 scoring** (`apps/customers/scoring.py`, `nba.py`; migration `customers/0002`):
  - Features: record, source, event, order and ticket counts; monetary, AOV, recency, tenure, RFM r/f/m, days_since_last_event. No PII.
  - `POST customers/features/publish {kind: current|training}`. `training` is a point-in-time set: features as of `as_of - churn_window_days` (setting, default 90), with labels `churned` and `future_value`.
  - `POST customers/scoring-runs {score_type: churn|clv|next_best_action (alias nba), model_id?}` returns 202 `{run_id, job_id, model_id, model_version, selection}`.
    - Model: the explicit one, or auto-selected (right task type, all required features available, the customer project's dataset first, production before staging).
    - Errors: 409 `ml_model_required` when no model can serve; 400 `ml_model_incompatible` for a wrong task type or missing features.
    - Predictions run through `apps.ml.services.predict_frame(..., explain=True)`.
    - Each `CustomerScore` stores the value (churn: probability, risk_band; CLV: predicted_value, horizon_days), the confidence, the model version and the explanation (drivers, features, model).
  - Every run then refreshes **next-best-action**: rules on the RFM segment, rule segments, churn band, CLV percentile and tickets.
  - `GET customers/scoring-runs?score_type=` lists the run history.
- **Fixes:**
  - A malformed connector host is now 400 `connector_host_invalid` (`InvalidHostConfig`).
  - Pipeline rename/derive names must match `^[a-z_][a-z0-9_]{0,62}$` (QA W4 defect 4).
  - The C360/P360 permission tests follow the W4 grants (Analyst = view+manage+review; Viewer = view).
  - The live-Postgres connector tests now derive the migrator URL from settings; before, they failed authentication.

## Tests (real Postgres, PYTEST_DB_SUFFIX=data_engineering)
- **Owned suites** (connectors, datasets, pipelines, quality, lineage, warehouse, semantic, sqlguard, customers, products): **1111 collected: 1109 passed, 0 failed, 2 errors** (junit).
  - The 2 errors were the live-Postgres connector tests. After the migrator-URL fix, that file passes 4/4 on its own.
  - New tests: a real ML training → promote → churn and CLV scoring end to end; the point-in-time training set; NBA rules (unit); step permission re-checks; ingest, run_succeeded and run_failed events; `pipelines.run` as the actor.
- **QA subset** (`tests/security/test_connector_ssrf.py`, `test_pipeline_dag_injection.py`, and the isolation matrix filtered to my routes): 672 tests, 671 passed. The 1 failure is `[rename-to-sql]` XPASS(strict): the defect is fixed (see below).
- **Checks:** check_conventions OK; check_ownership OK; ruff clean on owned dirs; manage.py check OK; makemigrations --check: no changes; import-linter 2/2 kept.

## Requests
- **qa_security:** remove the `xfail(strict)` mark on `tests/security/test_pipeline_dag_injection.py` `rename-to-sql`. NewName now has the identifier pattern.
- **backend (automation):**
  - Please expose `actor_id` on `StepContext` (my helpers also accept `user_id`/`actor`/`run.*`).
  - Steps raise DRF/`NexoraError` exceptions (`Forbidden`, `Conflict`, `ValidationError`, `MlModelRequired`). Map them to a failed step with the `code`.
  - Event payloads include `org_id`.
- **frontend:**
  - C360 scoring UI: `GET/POST customers/scoring-runs`, and `POST customers/features/publish {kind: "training"}` to create the training set.
  - `GET customers/{id}/scores` `latest[]` holds churn (`value.probability`, `risk_band`), clv (`value.predicted_value`) and next_best_action (`value.{action,title,channel,priority,description}`, `explanation.{rule,signals}`).
  - Settings now include `churn_window_days` (7-730) and `training_dataset_id`.

## Defects for others
- None new.

## Open issues
- None in owned apps.
