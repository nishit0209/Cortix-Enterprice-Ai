# Handoff: data_engineering, wave 3

Branch `agent/data_engineering/w3` (base `main` @ ce5969a). Owned apps: connectors, datasets, pipelines, quality, lineage,
warehouse, semantic, sqlguard. Nothing outside those apps (and this handoff) was edited. No new migrations, no new
dependencies, no new permission codenames.

## Done

### A. Local runtime (CONTRACT "Local runtime")
- **No Redis / S3 / Celery-beat assumptions in the data apps.** All async work goes through `TenantTask.delay/apply_async`.
  Storage errors are backend-agnostic (any fs/S3 failure -> `storage_unavailable`; `datasets.services` no longer imports
  botocore). Connector previews read in-process unless a Celery worker exists (`TASK_RUNNER="celery"`).
- **Schedule dispatcher.** `PipelinesConfig.ready()` calls `scheduling.register_local_dispatcher()`, which lazily imports
  `apps.core.scheduler.register_periodic` and registers `("pipelines.dispatch", 60, scheduling.local_tick)`. An ImportError
  logs a WARNING ("apps.core.scheduler is not available ...") and skips; it disappears once Backend W3 is merged.
  - The django_celery_beat PeriodicTask (`pipelines.dispatch_due`, migration 0002) is kept, but the beat task is a no-op
    unless `TASK_RUNNER="celery"`, and `local_tick` is a no-op under Celery. Exactly one path dispatches for every runner
    (unit-tested), and every pass also holds `pg_try_advisory_lock(hashtext('nexora:pipelines.dispatch'))`, so two
    processes never dispatch concurrently (schedules/runs are still claimed row by row with SKIP LOCKED).
  - Thread runner (no durable delayed queue): the dispatcher pass also starts queued retries whose backoff elapsed
    (`next_attempt_at` is pushed out as a lease), re-dispatches queued runs lost with the server (untouched for longer than
    `PIPELINE_HEARTBEAT_TIMEOUT_S`), and finalizes runs whose output ingestion finished. Every runner additionally
    finalizes a run as soon as its output is ingested, via the new `apps.datasets.signals.dataset_ingest_finished`
    (sent outside any transaction once an ingest job is terminal). `execute_run`/`finalize_run` stay idempotent.
  - Warehouse org provisioning is dispatched after commit for non-eager runners.
- **Warehouse on plain PostgreSQL 16/17** (`scripts/bootstrap_local_db.py`): verified against PG16+ CREATEROLE rules.
  `wh_loader` (CREATEROLE, not superuser, owner of `nexora_wh`) creates each `wh_q_<id>` reader role itself, so it holds
  ADMIN OPTION on it, and grants it to the NOINHERIT `wh_executor` (membership with SET, without INHERIT). Nothing in the
  provisioning SQL needs a superuser. A reader role the loader cannot administer (e.g. created by a superuser) fails with
  `warehouse_role_not_administrable` and an actionable message. New `manage.py check_warehouse [--provision-test]`
  (catalog checks; the provision test creates, uses through `SET ROLE` and drops a throwaway schema + role).
- **Large ingest.** COPY into the warehouse casts and serialises per 50,000-row batch (no second full copy of the frame);
  warehouse reads spool COPY output to a temp file; the all-text frame is released after analysis; type inference prunes
  ladder rungs with an exact bound and casts dates/timestamps with the shortest prefix of the format family that holds
  every format seen in the sample, then retries only failed rows (first-match order kept: results equal a single pass).
  Perf test `apps/datasets/tests/test_perf_large_ingest.py` (marked `slow`, pure): **1,000,000-row CSV (46.9 MB):
  generate 0.08 s | parse 0.06 s | infer + profile + PII 1.24 s | COPY serialisation 20 x 50,000 rows 0.13 s (54.9 MB)**.

### B. Notifications (lazy `apps.core.notifications.notify`; logged no-op while missing; savepoint; never raises)
| Event | Recipient | kind | link |
|---|---|---|---|
| pipeline run succeeded | pipeline owner | `pipeline_run.succeeded` | `/o/<org>/data/pipelines/<id>/runs/<run>` |
| pipeline run failed (respects `notify_on_failure`) | pipeline owner | `pipeline_run.failed` | same |
| connector import finished / failed | user who started the import | `import.finished` / `import.failed` | dataset / `data/sources` |
| dataset version ready / blocked by a quality gate / ingestion failed | uploader (fallback: dataset owner) | `dataset.ready` / `dataset.failed` | `/o/<org>/data/datasets/<id>` |

Pipeline outputs pass `options={"notify": False}` to ingestion, so a run produces exactly one notification.

### C. Frontend W2 requests, plus a check of `frontend/src` against the backend (backend side fixed, all additive)
- `POST /pipelines/{id}/validate` (new): validates the posted `dag` (or the current version) and, when valid, dry-runs
  it on a 1,000-row sample. Always 200: `{valid, ok, errors, columns, rows, row_count, steps, schemas, order, node_id,
  source_rows?, rows_rejected?, runtime_error?}`. Errors: `{node_id, node, field: "nodes.<id>.<field>", code, message}`;
  a step failing on the sample is pinned as `nodes.<id>.data`. `POST /pipelines/validate` uses the same error shape.
- `POST /pipelines/{id}/preview` takes optional `node_id` (output after that node; later steps `skipped`) and `limit`
  (1..200). Unknown node -> 400 `{errors:[{field:"node_id"}]}`.
- `GET /lineage`: every node has `restricted: bool`; nodes the viewer may not see (dataset ACL `read`, `pipelines.view`,
  `sources.view`) are collapsed into ONE `{id:"restricted", type:"restricted", restricted:true}` placeholder and never
  traversed; adds `restricted_count`. A restricted root is still 404.
- Pipeline builder dialect (`frontend/src/lib/pipelines/catalog.ts` + `inspector.tsx`) is accepted as written:
  node types `source` / `destination` (+ the DATA_ENGINEERING_DESIGN dotted names), `cast.casts[{column,to}]`,
  `filter.mode` and ops `= != > >= < <=`, `deduplicate.keys`, `aggregate.aggregations[{column,fn,as}]` (`avg`->`mean`),
  `sort.by[{column,desc}]`, `fill_null.strategy:"constant"`, `destination.dataset_name`, `derive.expression` as TEXT
  (safe recursive-descent parser -> the whitelisted JSON tree, still type-checked), edge `port`, DAG `settings`, and the
  two-input join (a dataset source on the join's right handle is folded into `config.dataset_id`). The stored DAG keeps
  the builder form. `destination.mode:"append"` is refused on its node (`unsupported_mode`).
- Schedules accept/return `is_active` (alias of `enabled`); run steps add `step_key`/`step_type`; log lines add
  `step_key`; run `quality_summary` adds `rules_passed`/`rules_failed` (also in the ingest job result).
- Data sources: import accepts the catalog browser's `table: "schema.table"` (split only when the stored catalog lists it,
  or the prefix is the configured schema); connection tests add `checks: [{key, ok, message}]` (`dns -> ssrf -> tls ->
  login -> readonly`, only the checks that ran); the catalog adds `row_estimate` (alias of `estimated_rows`).
- Semantic tab: `GET/POST /semantic-models` (`?dataset_id=` generates on first access), `GET/PATCH /semantic-models/{id}`,
  `POST /{id}/generate`, `POST /{id}/publish`, metrics CRUD under `/{id}/metrics[/{metric_id}]`. UI shape: `alias`,
  `column_name`, `status:"published"` while ready (`model_status` = raw); PATCH with the full table list applies only
  changed fields (untouched values never become user edits); role `attribute` = `dimension`. Dataset ACLs apply.

### D. Isolation factories
Every resource route of my apps resolves a factory (new: `pipeline-validate-detail`, `semantic-model-*`). QA's
`test_every_resource_route_has_an_isolation_factory` and `test_isolation_registrations_match_live_routes` pass on this
branch (they run without a DB).

### E. Dashboard KPIs (`GET /data-stats`, all from the DB; additive fields)
`datasets`, `datasets_by_status{ready,processing,failed}`, `rows_total`, `avg_quality` (0-100, over scored datasets, null
when none), `storage_bytes`, `pipelines_active`, `pipelines_total`, `runs_7d{succeeded, failed, total}`, `runs_active`,
`sources`, `sources_active`. Each provider runs in a savepoint; defaults are zeros, never invented values.

### F. sqlguard
- All W2 behaviour kept; `mask_null` stays `CASE WHEN FALSE THEN col END` (approved).
- `mask_hash` now follows SECURITY_ARCHITECTURE §5: `encode(sha256(convert_to(col::text || <org salt param>,'UTF8')),'hex')`
  (was MD5); users still get `forbidden_function` for md5/sha*/encode/convert*/repeat.
- QA corpus on this branch (`pytest tests/security/sql`): **183 passed, 9 failed, 2 skipped**. All 9 failures
  (mask-002/003/004/007/008/009/010/012, setop-009) are the oracle rejecting the approved `mask_null` rendering; no
  guard bug found. The 2 skips are EXPLAIN-time cases covered by the warehouse suite.

## Tests (REAL counts, 2026-11-20, from `backend/`)
- My 8 apps, `pytest apps/{connectors,datasets,pipelines,quality,lineage,warehouse,semantic,sqlguard} -m "not slow"`:
  **862 passed, 159 errors, 1 deselected** (the perf test, run separately: **1 passed**).
  - The **159 errors are all DB-backed tests: NOT EXECUTED, no database.** A PostgreSQL answers on localhost:5432 but
    rejects `nexora_migrator` (the user has not run `scripts/bootstrap_local_db.py` yet); every error is the
    `OperationalError: password authentication failed` at DB setup. 39 of them are new W3 DB tests (pipelines 17,
    datasets 7, connectors 5, semantic 5, warehouse 3, lineage 2).
  - New W3 unit tests: 128 (pipelines 65, connectors 24, datasets 14, warehouse 14, semantic 6, lineage 5).
- QA `tests/isolation/test_endpoint_matrix.py` registry/discovery tests (no DB): 23 passed, 1 skipped (`exports/{id}`
  not routed yet, not mine).
- QA SQL corpus: 183 passed, 9 failed (oracle, see F), 2 skipped.
- Checks: `check_conventions.py` OK; `check_ownership.py --agent data_engineering --base main` OK; `ruff check` on my
  apps OK; `manage.py check` 0 issues; `makemigrations --check` no changes.

## Requests
- **Backend:** keep `apps.core.scheduler.register_periodic(name, every_seconds, fn)` and
  `apps.core.notifications.notify(user_ids, kind, title, body, link)` exactly (I pass a list of UUIDs, title <= 200,
  body <= 1000 chars). With Celery, `pipelines.dispatch` from the registry is a no-op by design (beat runs
  `pipelines.dispatch_due`). The fs storage `download_to` should stream (copyfileobj), ingestion relies on it for large
  files.
- **Integrator:** fold the additive W3 shapes above into CONTRACT.md (validate dry run, preview `node_id`, lineage
  `restricted`, data-stats fields, data-source `checks`/`row_estimate`/`schema.table`, `/semantic-models`, builder DAG
  dialect, `mask_hash` sha256, notification kinds). Manual guide: after `bootstrap_local_db.py` + `migrate`, run
  `manage.py check_warehouse --provision-test` to prove the warehouse roles.
- **QA:** accept the approved `mask_null` rendering in the oracle (the 9 corpus failures), then run my 159 DB tests
  once the database exists.

## Open issues
- 159 DB-backed tests have not run (no database). Their API/import/runner/notification end-to-end results are unknown.
- No previous-period values in `data-stats` (the dashboard hides KPI deltas); `previous` would need history tables.
- Pipeline outputs cannot append (the builder's "Append" write mode is refused per node).
- Under the thread runner, retry backoff and the fallback finalize resolve on the 60 s dispatcher tick (the
  ingest-finished signal finalizes immediately).
- Until Backend W3 is merged: in-app notifications are skipped (logged) and every process start logs the scheduler
  WARNING.

## Defects for other agents
- **Frontend** (the backend now accepts all of these; the canonical forms are recommended):
  - `src/lib/pipelines/catalog.ts:243`: the destination "Append" write mode does not exist; the backend refuses it with
    `unsupported_mode` on the node. Remove the option (every run writes a new version).
  - `src/lib/pipelines/dag.ts:62` `serializeDag` writes the builder dialect (`source`, `casts`, `keys`, `fn`/`as`, `desc`,
    `dataset_name`, text expressions, join right input as a second source). Canonical: `source.dataset`,
    `destination.dataset{name}`, `cast{columns:{col:type}}`, `filter{match, op: eq|ne|gt|gte|lt|lte|...}`,
    `deduplicate{subset}`, `aggregate{aggregations:[{func,column,alias}]}`, `sort{by:[{column,descending}]}`,
    `fill_null{strategy:"value"}`, `derive{expression: JSON tree}`, `join{dataset_id, how, on}`.
  - `src/components/sources/sources-view.tsx:679` sends `table: "schema.table"`; send `{schema, table}` instead.
  - `src/lib/types.ts:305` `DataStats` and `:575` `LineageNode` can use the new fields (`sources`, `runs_7d.total`,
    `datasets_by_status`; `restricted`, `restricted_count`).
- **QA:** `tests/security/sql` oracle rejects the approved `mask_null` rendering
  (`tests/security/sql/test_sql_guard_corpus.py:239`, 9 cases).
