# Handoff — backend — wave 3

Branch: `agent/backend/w3`   Base: `main` (all W1 + W2 work merged)   Contract: CONTRACT.md "Local runtime: PostgreSQL only, no Docker", "W2 as-built additions" and "W3 additions" implemented as binding.

Note: this run was interrupted once by a usage limit and resumed. No existing commits were rewritten. The uncommitted work from the first run (runner/storage edits + three unit-test files) was reviewed, made to pass and committed as `55db137`.

## Done

### A. Local runtime in apps/core (`9e51a4e`, `55db137`)
1. **Task runner** (`apps/core/runner.py`, `RunnerMixin` in `TenantTask` and the new non-tenant `NexoraTask`). `.delay()` / `.apply_async()` honour `settings.TASK_RUNNER`; callers never branch.
   - `celery`: normal Celery.
   - `eager`: inline. `self.retry()` is honoured immediately (no countdown); exceptions propagate.
   - `thread`: process-wide `ThreadPoolExecutor(TASK_RUNNER_THREADS)`. Each run starts in a fresh `contextvars.Context` and calls `close_old_connections()` before and after. `TenantTask.__call__` enters `tenant_context(org_id)` and restores the actor. `countdown`/`eta` and `self.retry(countdown=...)` go through a daemon timer. Exceptions are logged, and the task's job (`job_id` + `org_id` kwargs) is marked failed. An immediate nested submit from inside a pool thread runs inline, which avoids a pool dead-lock.
   - An eager `TenantTask` started from `transaction.on_commit` opens its own tenant transaction.
2. **Job events.** `core.JobEvent` (`org`, `job_id`, `seq`, `event`, `data`, `created_at`) has RLS and is append-only. Only `nexora_ops` may delete.
   - `jobs.publish()` writes rows on the `durable` alias when `JOB_EVENTS_BACKEND="db"`. `seq` is allocated per job under `pg_advisory_xact_lock`. The Redis path is unchanged.
   - The SSE view polls rows with short `sync_to_async` queries, each in its own tenant transaction. `Last-Event-ID` = `seq`. Polling backs off from 0.5 s to 2 s. The `retry: 3000`, `:hb` and terminal `done` behaviour is unchanged.
   - `apps.core.ops.purge_job_events` deletes rows older than 7 days in batches on the `ops` alias. It is registered as periodic job `core.purge_job_events` (every 6 h).
3. **Storage.** `STORAGE_BACKEND="fs"` sits behind the unchanged public API of `apps.core.storage`.
   - Files live under `LOCAL_STORAGE_DIR/<key>`. Key validation is the same as before, plus Windows tricks (reserved names, `:`, trailing dot/space) and symlink escape checks.
   - Writes are atomic: a `.tmp-*` file, then `os.replace`.
   - `presigned_get` returns `/api/v1/files/<token>`, a `TimestampSigner(STORAGE_URL_SIGNING_KEY)` token whose `max_age` is the expiry (capped at 7 days).
   - The token is served by `apps/core/urls_public.py` → `apps.core.files.download`: a streaming `FileResponse` with an RFC 6266 `Content-Disposition`, `nosniff` and `no-store`. It answers 404 for an invalid token or a missing object, 410 `download_expired`, and 404 when the backend is S3.
   - `guess_content_type` uses a stable table for the formats NEXORA serves, because the Windows registry maps `.csv` to Excel.
   - **AI/ML W2 defect fixed:** `apps.core.tenancy.tenant_scope(org_id)` (also exported as `storage.org_scope`) is a ContextVar-only tenant scope for storage I/O without a DB transaction.
4. **Periodic jobs** (`apps/core/scheduler.py`).
   - `register_periodic(name, every_seconds, fn)` validates its arguments; registering a name again replaces it.
   - With `LOCAL_SCHEDULER`, one daemon thread ticks every 15 s, in server processes only: the runserver child (`RUN_MAIN`), `--noreload`, uvicorn, daphne and gunicorn. It never runs for migrate, tests, shell or other commands. `NEXORA_SCHEDULER=0` disables it and `=1` forces it.
   - Each tick is guarded by `pg_try_advisory_lock`. Last-run times are shared through the cache, with an in-memory fallback.
   - Celery beat: `beat_init` installs `nexora:<name>` entries that run `apps.core.scheduler.run_periodic`.
5. **Health.**
   - `/healthz` (always 200) and `/readyz` (503 + `status: "degraded"` on any error) return `checks: {database, cache, redis, storage}` and a `runtime` block.
   - Redis without `REDIS_URL` → `"not_configured"`. Storage → `"fs"` / `"s3"`. The database check is a real `SELECT 1`.
   - Error values are `error: <ExceptionType>` only; no hosts or users are exposed.
6. **Cache.** With `DatabaseCache`, `readyz` checks `to_regclass(<table>)` first. A missing table reports `error: cache table 'nexora_cache' is missing (run manage.py createcachetable)` without querying it. Tests cover this in `717ad3c`.

### B. `manage.py seed_demo` (`2caa3a6`; apps/platform_admin/demo.py)
- Options: `--email` (default `demo@nexora.local`), `--password`, `--org-name`/`--org`, `--reset`, `--data-dir`.
- Idempotent: the state is kept in the org's `settings.demo_seed`.
- New user without `--password`: a strong 20-character password is generated and printed. An existing user keeps their password unless `--password` is given. The org's AI policy stays `synthetic_only`.
- Every top-level CSV of `testdata/generated/` is ingested (sales first; never `adversarial/`). If there are none, `testdata/generate.py --out <dir>` runs via subprocess.
- Ingestion goes through `datasets.services.create_upload` + `create_dataset` (classification `synthetic`, `attest=True`). `TASK_RUNNER` is forced to `eager` inside the command and restored afterwards.
- One sample conversation ("Sales overview") is answered by the REAL chat pipeline. The deterministic builder answers all five questions, so no Gemini key is needed. The default dashboard has 5 widgets pinned from those answers: Total revenue (kpi), Revenue by region, Monthly revenue trend, Top 10 products by revenue, Revenue by channel.
- `--reset` soft-deletes the demo datasets and deletes the dashboard (and its pinned queries) and the conversation first.
- It prints login, org slug, AI policy, a per-dataset table (status/rows/cols/quality/action), total rows and the dashboard.

### C. Dashboards (`4911f2e`) and Notifications (`9e51a4e`, `fb240bf`)
**Dashboards** (apps/bi: `Dashboard`, `DashboardWidget`, TenantModels with RLS, migration `bi/0002_dashboards`). Permissions are `dashboards.view` (all system roles) and `dashboards.manage` (owner, admin, data_engineer, analyst, data_scientist). Both are in the catalog and seeded into roles. Shapes as built:
- `GET /dashboards`: the standard list envelope `{items, next_cursor, previous_cursor}` (CONTRACT "Lists"; `?limit=`; ordered by `-updated_at`). Each item is `{id, title, description, is_default, is_shared, owner_id, widget_count, created_at, updated_at}`. The list holds the caller's own dashboards plus shared ones.
- `POST /dashboards {title, description?, is_default?, is_shared?}` → 201 with the full dashboard. A user's first dashboard becomes their default (one default per owner).
- `GET /dashboards/{id}` → summary + `widgets: [{id, title, kind, saved_query_id, dataset_id, chart, layout:{x,y,w,h}, text, created_at, updated_at}]`, sorted by (y, x).
- `PATCH /dashboards/{id} {title?, description?, is_default?, is_shared?, layout?: [{id,x,y,w,h}]}` and `DELETE`. Only the owner may change a dashboard: a shared one answers 403 to others, an invisible one 404.
- `POST /dashboards/{id}/widgets {title, kind, saved_query_id? | message_id?, chart?, layout?, text?}`.
  - `message_id` pins an Ask-AI answer: a `SavedQuery` is created from the AUDITED `raw_sql` and re-validated under the caller's policy.
  - The grid is 12 columns, with x + w ≤ 12 and h between 1 and 24. Without a layout, the widget is placed below the existing ones (w 6, h 4).
  - `chart`: `{type: bar|line|area|pie|scatter|table|kpi, x?, y[], series?}`. A kpi widget without a chart gets `{type: kpi}`.
  - At most 50 widgets.
- `PATCH` / `DELETE /dashboards/{id}/widgets/{wid}`.
- `GET /dashboards/{id}/widgets/{wid}/data` → `{columns, rows, row_count, truncated, chart, refreshed_at, query_audit_id, dataset_id}` with `Cache-Control: no-store`.
  - Data is re-executed through `sqlguard.validate` + `execute` under the VIEWER's `effective_policy` and never comes from stored rows. Each refresh writes a `BiQueryAudit` row (purpose and source `dashboard`) and AccessHistory.
  - Errors: 404 when the viewer cannot query the dataset; 422 guard rejection (`code` + `query_audit_id`; the audit row survives the 4xx); 409 `widget_has_no_data`, `widget_query_missing`, `too_many_widgets`, `pin_not_available`.
- Every write is audited (`bi.dashboard.*`, `bi.dashboard.widget.*`, `bi.saved_query.create` for pins).

**Notifications** (core: `Notification` TenantModel with RLS, migration `core/0002`):
- `GET /notifications?unread=1|0&limit=` → `{items:[{id, kind, title, body, link, read_at, created_at}], next_cursor, previous_cursor, unread_count}` (own only, newest first).
- `POST /notifications/{id}/read` → the notification + `unread_count` (another user's id → 404; audited).
- `POST /notifications/read-all` → `{updated, unread_count}`.
- `apps.core.notifications.notify(user_ids, kind, title, body="", link="")` → number created.
  - One row per distinct valid user. Links must be app-relative paths; anything else is dropped. Delivery failures never break the producer (savepoint).
  - `notify_permission(codename, kind, title, body, link)` notifies every ACTIVE member holding a permission. `apps.orgs` registers the lookup.
  - Known kinds are in `notifications.KINDS`.
- **Producer in backend:** accepting an invitation notifies the inviter and every member with `members.manage`, never the joiner (`invitation.accepted`, link `/settings/members`).

### D. bi changes (`4911f2e`)
- `ports.propose` / `remember` / `store_verified` pass `semantics=DatasetSemantics`, so the nl2sql cache is scoped to the dataset version.
- The `ai_quota_exhausted` error event includes `resets_at`. It comes from the proposal, else from `GET ai/status` `quota.resets_at` → `resets_at` → `budget_resets_at`.
- The SSE and JSON shapes are otherwise unchanged.

### E. Isolation factories
Every backend-owned tenant route with resource kwargs has a factory that creates a REAL resource:
- orgs: member-detail, invitation-detail; accounts: api-key-detail (`apps/orgs/tests/isolation.py`).
- governance: policy-preview, user-attributes, dataset-acl/column-policy/row-policy list + detail.
- bi: conversation detail/messages, message feedback, saved-query detail/run, dashboard detail/widgets, widget detail/data.
- core: notification-read. Jobs are covered by `testing.isolation`.

`test_every_resource_route_has_an_isolation_factory` and `test_isolation_registrations_match_live_routes` pass.

### F. Tests (`55db137`, `717ad3c`, `f4783f7`, `373dd5c`, `9d3ddc4`)
Unit tests (fakes, no DB):
- runner: thread/eager/celery routing, fresh context, connection hygiene, retries through the timer, exhausted retries mark the job, nested submit, real pool round trip.
- scheduler: due times, lock acquired / not acquired, failure isolation, server-process detection, daemon thread, beat bridge.
- fs storage: traversal + Windows tricks, atomic write, signing, expiry, tamper, key rotation, streaming view, `tenant_scope`.
- health: missing cache table, unreachable DB/Redis, unwritable storage, S3 bucket check.
- SSE DB stream: replay, back-off, keep-alive, lifetime.
- notifications: link sanitising, no recipients, failing lookup.
- seed_demo:
  - password strength, CSV discovery, and a REAL generator subprocess run;
  - summary and command wiring/errors;
  - the deterministic builder answers every dashboard question over the sales schema.

DB tests (written; not executed, no database):
- JobEvent: seq, lifecycle, RLS raw SQL, append-only, SSE replay after `Last-Event-ID` via the API, purge on the ops alias (transactional).
- Notifications API: own-only, unread filter, cursor pages, read, read-all, audit, cross-user and cross-org 404.
- Invitation-accepted producer.
- Dashboards API: CRUD, defaults, bulk layout, widget validation (10 cases), 50-widget cap, sharing (404 vs 403), permissions for viewer/auditor/anon, cross-org IDOR and foreign widget ids, RLS raw SQL.
- Widget data: re-executed on every refresh (changed results are visible, audits with source `dashboard`), a RESTRICTED VIEWER (column deny + mask + row policy) gets HIS policy, dataset ACL → 404, guard rejection 422 with a surviving audit, text widget / deleted query 409, pinning from the audited SQL, foreign or failed answers.
- `seed_demo` end to end: eager, real services, no Gemini key; live widget data via the API; idempotent re-run; `--reset --password`.
- `/readyz` against the real DB; `to_regclass` on a missing table.

## Tests (REAL counts, this run; Python 3.12.7, Django 5.2.19, Windows)
A PostgreSQL server listens on :5432, but the NEXORA roles and databases do not exist yet. Every DB-backed test therefore ERRORS at setup with `password authentication failed for user "nexora_migrator"`. These are "not executed: no database", not passing.

| Suite | Passed | Failed | Skipped | Errors (no DB) |
|---|---|---|---|---|
| apps/core/tests/test_runner.py | 18 | 0 | 0 | 0 |
| apps/core/tests/test_scheduler.py | 31 | 0 | 0 | 0 |
| apps/core/tests/test_storage_fs.py | 30 | 0 | 1 (symlinks need privilege on Windows) | 0 |
| apps/core/tests/test_health.py | 10 | 0 | 0 | 2 |
| apps/core/tests/test_job_events.py | 14 | 0 | 0 | 9 |
| apps/core/tests/test_notifications.py | 15 | 0 | 0 | 8 |
| apps/orgs/tests/test_invitation_notifications.py | 0 | 0 | 0 | 2 |
| apps/bi/tests/test_dashboards.py | 0 | 0 | 0 | 24 |
| apps/platform_admin/tests/test_seed_demo.py | 18 | 0 | 0 | 1 |
| **All owned apps** (core, accounts, orgs, governance, bi, platform_admin, automation) | **249** | **0** | **1** | **204** |
| tests/isolation/test_endpoint_matrix.py | 21 | 0 | 3 (W2 routes of other apps not routed yet) | 947 |
| Whole backend suite (for reference) | 1995 | 13 (none backend-owned, see Defects) | 10 (+1 xfailed) | 1482 |

Checks:
- `scripts/check_conventions.py` → conventions OK.
- `scripts/check_ownership.py --agent backend --base main` → ownership OK.
- `ruff check` on all owned apps → passed.
- `manage.py check` → no issues.
- `makemigrations --check --dry-run` → No changes detected. It warned that it could not verify the history without the DB.
- `lint-imports` → 2 contracts kept, 0 broken.

## Requests
- **qa_security** (`testing/fixtures.py`): the `fake_redis` fixture should also set `settings.JOB_EVENTS_BACKEND = "redis"`. Test settings now default to `"db"`, so `tests/isolation/test_jobs_sse_isolation.py::test_publish_writes_only_under_the_active_org_prefix` and `::test_job_lifecycle_events` will fail once a DB exists: they assert on Redis streams that are never written. Alternatively, parametrize them over both backends. The DB path is covered by `apps/core/tests/test_job_events.py`.
- **qa_security** (`tests/security/test_storage_keys.py`): the `s3` fixture must set `settings.STORAGE_BACKEND = "s3"`. Test settings default to `fs`. `test_own_prefix_operations_reach_s3` and `test_presigned_filename_is_sanitised` fail now. I verified that all 12 tests pass with `TEST_STORAGE_BACKEND=s3`.
- **data_pipelines**:
  - call `notify([owner_id], "pipeline.run_failed" | "pipeline.run_succeeded", title, body, "/pipelines/<id>/runs/<run_id>")` at run end;
  - call `"import.finished"` / `"import.failed"` for connector imports;
  - register the schedule dispatcher with `apps.core.scheduler.register_periodic("pipelines.dispatch_schedules", 60, fn)` in `AppConfig.ready()`.
- **data_engineering**: call `notify([uploader], "dataset.ingest_finished" | "dataset.ingest_failed", ...)` (link `/datasets/<id>`) when ingestion ends.
- **ai_ml**:
  - documents → `"document.processed"` / `"document.failed"`;
  - AI quota exhausted → `notify_permission("org.settings.edit", "ai.quota_exhausted", title, body, "/settings/ai")`;
  - keep `GET /ai/status` `quota.resets_at`, which `bi.ports.ai_quota_resets_at` reads.
- **frontend**: shapes as built in C. `GET /dashboards` is the standard list envelope, not a bare array. Notifications lists add `unread_count`.
- **Integrator**:
  - Once the user creates the DB, run the owned DB suites: `$env:PYTEST_DB_SUFFIX="backend"; pytest apps/core apps/accounts apps/orgs apps/governance apps/bi apps/platform_admin --create-db`.
  - The final manual guide must list `manage.py migrate` → `manage.py createcachetable` (readyz reports the missing table otherwise) → `manage.py seed_demo` (prints the demo password).

## Open issues
- Nothing DB-backed ran in this wave (no database). Those results are unknown, not passing.
- The `seed_demo` end-to-end test is transactional (real commits). Like the other warehouse tests, it leaves its warehouse schemas (prefix `t_backend_`) in `nexora_wh`.
- `test_symlinked_directory_cannot_escape_the_root` is skipped on Windows accounts without the symlink privilege. The code path is still checked by the resolve-and-compare logic.

## Defects for other agents
- **qa_security**: the two fixture issues listed under Requests (`fake_redis` vs DB job events; `s3` fixture vs fs default).
- **qa_security / data_engineering**: `tests/security/sql/test_sql_guard_corpus.py` cases `mask-002/003/004/007/008/009/010/012` and `setop-009`, and `tests/security/sql/test_sql_guard_fuzz.py::test_differential_accepted_queries_reparse_and_stay_in_policy`, fail. This branch does not touch `apps/sqlguard` or those tests. The mask cases match the CONTRACT "Guard masks" decision: the oracle must accept `CASE WHEN FALSE THEN col END`.
- **qa_security**: `tests/isolation/test_tenancy_core.py::test_guc_is_transaction_local_on_a_reused_connection` opens a raw psycopg connection, so it FAILS (rather than errors) without a database. This is environmental only.
