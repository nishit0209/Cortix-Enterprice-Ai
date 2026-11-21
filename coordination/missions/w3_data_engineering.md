You are the ONLY Data Engineering agent this wave. You own `backend/apps/{connectors,datasets,pipelines,quality,lineage,warehouse,semantic,sqlguard}/**` (NOT customers/products this wave).

A. **Local runtime** (CONTRACT "Local runtime").
   - Your apps must make no direct assumptions about Redis, S3/boto or Celery beat. Use `apps.core.storage`, `apps.core.jobs` and `TenantTask` (`.delay` works under every `TASK_RUNNER`).
   - **Pipelines schedule dispatcher.**
     - Register it with `apps.core.scheduler.register_periodic("pipelines.dispatch", 60, fn)`. The Backend agent builds `apps.core.scheduler` IN PARALLEL with exactly that signature.
     - Import it lazily in `AppConfig.ready()`. On ImportError, log a warning and skip.
     - Keep the django_celery_beat PeriodicTask path, but only for `TASK_RUNNER="celery"`. Make sure the two paths can never double-dispatch.
   - **Warehouse on a plain local PostgreSQL 16/17**, created by `scripts/bootstrap_local_db.py` (read it).
     - Verify that the provisioning SQL works with PG16+ CREATEROLE semantics: `wh_loader` creates the `wh_q_<id>` roles and grants them to `wh_executor` (NOINHERIT).
     - Fix anything that assumes the old Docker init.
   - **Large ingest from fs storage.**
     - Stream it (Polars scan / batched COPY). Never load whole files into memory when that can be avoided.
     - Add a perf test (marked slow, pure) that parses and profiles a generated 1M-row CSV, and report the timing.

B. **Notifications.** Call `apps.core.notifications.notify(user_ids, kind, title, body, link)` for these events:
   - pipeline run failed / succeeded → the pipeline owner;
   - connector import finished / failed;
   - dataset ingestion ready / failed → the uploader.

   The Backend agent builds `notify` in parallel: import it lazily, and skip if it is missing.

C. **Frontend W2 requests.**
   - `POST /pipelines/{id}/validate` (and `/pipelines/validate`) return per-node errors that name node ids.
   - `POST /pipelines/{id}/preview` accepts an optional `node_id` and `limit`.
   - `GET /lineage` marks restricted nodes.
   - Check against the frontend code on main (`frontend/src`) that request and response shapes match. Fix the backend side if they don't, and list frontend-side mismatches as defects.

D. **Isolation factories.** Add one for EVERY tenant route with resource kwargs in your apps: datasets, versions, columns, uploads, quality rules/results, semantic models/metrics, data-sources, imports, pipelines, pipeline versions, schedules, runs, lineage.
   QA's `test_every_resource_route_has_an_isolation_factory` must pass for your apps.

E. **Dashboard KPIs.** Make sure `GET data-stats` (or the existing stats endpoint) returns REAL numbers for the dashboard, all from the DB: datasets, total rows, average quality score, active pipelines, runs in the last 7 days, failed runs, sources.

F. **sqlguard.**
   - Keep every W2 behaviour.
   - The `mask_null` rendering `CASE WHEN FALSE THEN col END` is approved (SECURITY_ARCHITECTURE §5 table).
   - Run QA's corpus on main (`pytest tests/security/sql`) and report the counts.
   - Fix real guard bugs the corpus finds. Do not change the guard to match a mistaken oracle expectation.

G. **Tests** for all of the above: unit tests where possible, DB tests written even if they cannot run yet.
