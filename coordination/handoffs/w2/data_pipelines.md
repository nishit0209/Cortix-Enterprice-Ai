# Wave 2 handoff: data_pipelines (second Data Engineering agent)

Branch: `agent/data_pipelines/w2`. Ownership: `backend/apps/{connectors,pipelines,lineage}/**` only.
A usage limit interrupted the first run. This resumed run kept every existing commit, reviewed and committed the
uncommitted DB-backed tests, fixed one ruff import-order issue, and ran all checks again.

## Done

### A. Connectors (`apps/connectors`)
- **Models.** `DataSource`, `Credential` and `SourceImport` are TenantModels with RLS (migration `0001_initial`).
  - The credential is envelope-encrypted with `apps.core.crypto.encrypt_secret`. Its AAD is `org:<id>:credential:<id>`.
  - The password is write-only. It never appears in serializers, logs, audit metadata or error messages.
- **Plugin interface.** `base.py` defines `BaseConnector` with `test()`, `introspect()`, `read_batches(...)` and `incremental_cursor(...)`, plus `TestResult`, `ReadonlyCheck`, `TableInfo`, `ColumnInfo`, `Batch` and `CursorState`.
- **PostgresConnector** (`postgres.py`, psycopg):
  - The host is resolved once by the SSRF guard in `ssrf.py`, which rejects private, loopback, link-local, multicast, reserved and unspecified addresses, plus the optional `CONNECTOR_DENY_CIDRS`, unless `ALLOW_PRIVATE_DB_HOSTS` is set.
  - The connection is pinned with `hostaddr` (defends against DNS rebinding) and uses `connect_timeout` and `statement_timeout`.
  - `sslmode` defaults to `require`; `verify-full` is supported.
  - Sessions are read-only transactions.
- **Read-only role check** runs on save and on test. It refuses superuser, BYPASSRLS, CREATE on the database or schema, and any INSERT/UPDATE/DELETE/TRUNCATE privilege on tables (`has_table_privilege`). The refusal code is `connector_role_not_readonly` and it lists each problem.
- **Endpoints.**
  - `GET/POST /data-sources` and `GET/PATCH/DELETE /data-sources/{id}`.
  - DELETE returns 409 `data_source_in_use` while a pipeline reads from the source.
  - `POST /test`, `POST /introspect` (schemas â†’ tables â†’ columns with types and estimated rows) and `POST /import` all return a 202 job on Celery queue `ingest`.
  - `GET /data-sources/{id}/imports` returns import history.
- **Import.** Rows stream in batches to a CSV in object storage. Then `apps.datasets.services.create_upload` + `create_dataset` run, or `add_version` on a re-import (incremental cursor supported), so profiling, quality, warehouse and semantic all use the standard path.
  - Lineage: `data_source â†’ dataset`.
  - Audit events: create, update, delete, test, introspect, import (requested/succeeded/failed).
- **Permissions:** `sources.view` and `sources.manage`.
- Lineage node resolver and asset-exists hook registered for `data_source`.

### B. Pipelines (`apps/pipelines`)
- **Models.** `Pipeline`, `PipelineVersion` (immutable DAG JSON), `PipelineRun`, `PipelineRunLog` (append-only) and `PipelineSchedule`, all with RLS.
  - `PipelineRun` has every contract field plus `number`, `trigger`, `parent_run`, `job_id` and a heartbeat.
- **DAG validation** (`dag.py`, pydantic). The DAG must be linear, with exactly one source and one destination, acyclic and connected. Validation also checks:
  - each node's config;
  - the referenced dataset or data source exists and is verified;
  - column-level schema propagation, giving per-node errors `{node_id, field, code, message}`.
- **Steps** (`steps.py`, pure Polars): select_columns, rename, cast, filter (structured conditions, no eval or SQL), fill_null, drop_nulls, deduplicate, trim_whitespace, change_case, derive, sort, aggregate, join.
  - `derive` uses `expressions.py`, a safe expression tree built from a whitelisted set of operators and functions.
- **Runner** (`runner.py`, TenantTask, queue `ingest`):
  - loads frames with `load_current_frame` or the connector;
  - records rows in/out and duration for each step;
  - writes the output as CSV â†’ `create_upload` â†’ `create_dataset`/`add_version`;
  - `finalize_pipeline_run` polls ingestion and copies the quality summary onto the run;
  - updates job progress.
- **Retries.** Retryable errors back off exponentially with jitter, up to `max_retries`. Non-retryable errors fail at once with an error code and message.
- **On failure:** an audit event, plus the `notify_run_failed` hook in `signals.py`, which emails the owner when `notify_on_failure` is set.
- A heartbeat timeout recovers runs whose worker was lost.
- **Scheduler** (`scheduling.py`, croniter, timezone-aware, minimum interval enforced):
  - Beat task `pipelines.dispatch_due` runs on the `ops` alias (BYPASSRLS dispatcher pattern). The cross-org scan reads ids only, then enqueues each run inside its org.
  - **Registration choice:** data migration `0002_beat_dispatcher` creates a django_celery_beat `PeriodicTask`, every 30 s, idempotent.
  - I chose a migration over `AppConfig.ready` so nothing writes to the DB at startup and operators can pause the entry.
- **Endpoints.**
  - Pipelines CRUD: `GET/POST /pipelines` and `GET/PATCH/PUT/DELETE /pipelines/{id}`. PATCH with a `dag` creates a new version.
  - `POST /pipelines/validate`.
  - `GET/POST /pipelines/{id}/runs` (POST returns a 202 job).
  - `GET /pipelines/{id}/versions`.
  - `GET/PUT/DELETE /pipelines/{id}/schedule`.
  - `POST /pipelines/{id}/preview` runs synchronously on the first 200 rows and writes nothing.
  - `GET /pipeline-runs` and `GET /pipeline-runs/{id}` (steps + logs).
  - `GET /pipeline-runs/{id}/logs`, `POST /pipeline-runs/{id}/retry` and `POST /pipeline-runs/{id}/cancel`.
- **Permissions:** `pipelines.view`, `pipelines.manage` and `pipelines.run`.
- **Stats provider** registered with `apps.datasets.services.register_stats_provider`. `data-stats` now reports `pipelines_active` and `runs_7d {succeeded, failed}`, which closes the W1 data_engineering open issue.

### C. Lineage (`apps/lineage`)
- Node types `data_source`, `pipeline` and `pipeline_run` added (migration `0002`). The `GET /lineage` contract is unchanged.
- Edges recorded for each run (keyed by run_id):
  - `data_source â†’ pipeline` and `dataset_version â†’ pipeline` (inputs);
  - `pipeline â†’ dataset`;
  - `pipeline_run â†’ dataset_version` and `pipeline â†’ dataset_version`.
- Resolvers registered for `pipeline` and `pipeline_run`.

## Tests (real counts)
Postgres (localhost:5433), Redis and MinIO were **down**: Postgres timed out on the port check and on the `makemigrations --check` consistency probe.

| Suite | Passed | Failed | Skipped | Status |
|---|---|---|---|---|
| connectors unit: `test_ssrf` 57, `test_postgres_unit` 42, `test_values` 3 | 102 | 0 | 0 | executed |
| pipelines unit: `test_steps` 47, `test_expressions` 46, `test_dag` 16, `test_engine` 7, `test_scheduling` 11 | 127 | 0 | 0 | executed |
| connectors `test_api` (21) + `test_live_postgres` (4) | â€“ | â€“ | â€“ | **not executed: infra unavailable** |
| pipelines `test_api` (16) | â€“ | â€“ | â€“ | **not executed: infra unavailable** |
| lineage `test_lineage` (5) | â€“ | â€“ | â€“ | **not executed: infra unavailable** |
| isolation matrix (factories in `connectors/tests/isolation.py` and `pipelines/tests/isolation.py`) | â€“ | â€“ | â€“ | **not executed: infra unavailable** |

- **Total executed:** 229 passed, 0 failed.
- **DB-backed tests written but not run:** 46, plus the isolation-matrix cases. They collect cleanly. I checked statically that their imports and the model fields the factories use resolve, and that every `pk` route has a factory.
- **Commands:**
  - Unit tests: `pytest apps/connectors/tests/test_ssrf.py apps/connectors/tests/test_postgres_unit.py apps/connectors/tests/test_values.py apps/pipelines/tests/test_{steps,expressions,dag,engine,scheduling}.py`.
  - Full run once Docker is up: `$env:PYTEST_DB_SUFFIX="data_pipelines"; python -m pytest apps/connectors apps/pipelines apps/lineage testing --create-db -p no:cacheprovider`.

**Other checks:** check_conventions OK, check_ownership OK (data_engineering), ruff clean, `manage.py check` no issues, `makemigrations --check` "No changes detected".

## Requests
1. **[contract]** Add "W2 as-built additions" to `contracts/api/CONTRACT.md`. Routes beyond the W2 table:
   - `GET/PATCH/DELETE /data-sources/{id}` and `GET /data-sources/{id}/imports`;
   - `/import` also accepts `{columns?, classification?, schema?, mode: full|incremental, cursor_column?, dataset_id? (re-import as new version), attest?}`;
   - `POST /pipelines/validate`, `GET /pipelines/{id}/versions`, `GET/PUT/DELETE /pipelines/{id}/schedule` and `POST /pipelines/{id}/preview`;
   - `GET /pipeline-runs`, `GET /pipeline-runs/{id}/logs`, `POST /pipeline-runs/{id}/retry` and `POST /pipeline-runs/{id}/cancel`;
   - DAG validation errors are `{node_id, field, code, message}`.
2. **[settings]** Optional settings, read with `getattr` defaults so nothing breaks if they are missing. Document them in `.env.example`/settings: `CONNECTOR_CONNECT_TIMEOUT_S`, `CONNECTOR_STATEMENT_TIMEOUT_S`, `CONNECTOR_DENY_CIDRS` (platform subnets, recommended in prod), `PIPELINE_MAX_ROWS`, `PIPELINE_HEARTBEAT_TIMEOUT_S` and `PIPELINE_MIN_SCHEDULE_INTERVAL_S`.
3. **[ops]** Run a celery beat process with the django_celery_beat DatabaseScheduler. The dispatcher entry goes to queue `default`, and `ingest` workers must exist for runs and imports.
4. **[integrator]** Run the DB suites above, including `testing/` isolation, once infra is up.

## Open issues
- The 46 DB-backed tests and the isolation-matrix cases have not run (infra down). Treat the API, import and runner end-to-end paths as unverified until they do.
- The failure notification is email-only for now. An in-app notifications app can connect to the same hook in `signals.py`.
- Only the `postgresql` connector type exists. The plugin interface is ready for more.

## Defects for other agents
- None found. Only the public functions in `apps.datasets.services` are used: `create_upload`, `create_dataset`, `add_version`, `load_current_frame` and `register_stats_provider`.
