# NEXORA API Contract (minimal, implementation-first)

> Integrator-owned. Backend apps implement; frontend consumes. Full reference: `docs/API_SPECIFICATION.md`.
> Live OpenAPI is generated from code at `GET /api/v1/schema`. **Shapes below are binding** for the current waves.
> Breaking changes are requested via your handoff file.

## Conventions
- Base: `/api/v1`. Tenant routes: `/api/v1/orgs/{org}/…` (`{org}` = slug). JSON, snake_case, ISO-8601 UTC, UUID strings.
- **Auth:** session cookie. The frontend first calls `GET /api/v1/auth/csrf` to set the `csrftoken` cookie, then sends `X-CSRFToken` on POST, PUT, PATCH and DELETE (with `credentials: "include"`).
- **Lists:** `{ "items": [...], "next_cursor": "<url>|null", "previous_cursor": "<url>|null" }` (cursor pagination, `?limit=`).
- **Errors:** `application/problem+json` `{ type, title, status, code, detail, request_id, errors?: [{field, code, message}] }`.
- **Visibility:** a resource in another org returns **404** (never 403).
- **Jobs:** an async operation returns `202 { "job_id", "status_url", "events_url" }`.
  - `GET /orgs/{org}/jobs/{id}` → `{ id, type, status, progress, message, result, error, created_at, finished_at }`.
  - **SSE:** `GET /orgs/{org}/jobs/{id}/events` sends `status`, `log`, `result`, `error` and `done` events, each with a JSON `data` field.

## W1: auth and orgs (Backend)
| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/auth/csrf` | – | 204, sets cookie |
| POST | `/auth/signup` | `{ email, password, full_name, org_name }` | 201 `{ user, org }` + session. The org is created with `ai_data_policy="synthetic_only"`. |
| POST | `/auth/login` | `{ email, password }` | 200 `{ user }` |
| POST | `/auth/logout` | – | 204 |
| GET | `/me` | – | `{ user: {id,email,full_name,is_platform_admin,timezone}, orgs: [{id,slug,name,role,status}] }` |
| POST | `/orgs` | `{ name }` | 201 `Org` (caller becomes owner) |
| GET, PATCH | `/orgs/{org}` | PATCH `{ name?, settings? }` | `Org = { id, slug, name, plan, status, region, ai_data_policy, created_at, role, permissions: [codenames] }` |
| GET | `/orgs/{org}/ai-policy` | – | `{ policy, gemini_tier, consent: {valid, version, granted_by, granted_at}|null, allowed_values: [...], consent_text_version }` |
| GET | `/orgs/{org}/ai-policy/consent-text` | – | `{ version, text }` |
| POST | `/orgs/{org}/ai-policy/consents` | `{ policy: "schema_only", consent_text_version, acknowledged: true, password }` | 201 (owner/admin, re-auth by password). Errors: `ai_policy_tier_forbidden`, `ai_policy_consent_required` |
| POST | `/orgs/{org}/ai-policy/revoke` | – | 200, policy is `synthetic_only` |
| GET, POST | `/orgs/{org}/members` | POST `{ email, role }` (invite) | `Member = { id, user: {id,email,full_name}, role, status, joined_at }` |
| PATCH, DELETE | `/orgs/{org}/members/{id}` | `{ role }` | last-owner protection → 409 |
| GET, POST | `/orgs/{org}/invitations` · DELETE `/{id}` | `{ email, role }` | `{ id, email, role, expires_at, status }` |
| POST | `/invitations/accept` | `{ token, password?, full_name? }` | 200 `{ org }` |
| GET | `/orgs/{org}/roles` | – | `[{ id, key, name, is_system, permissions: [..] }]` |
| GET, POST, DELETE | `/orgs/{org}/api-keys` | `{ name, scopes }` | create returns `{ id, name, prefix, secret }` **once** |
| GET | `/orgs/{org}/audit-events` | `?action=&target_type=` | list of `{ id, occurred_at, actor_type, actor_id, action, target_type, target_id, metadata }` |
| GET | `/orgs/{org}/activity` | – | recent audit events with actor email (dashboard feed) |

### W1 as-built additions (binding from W2; details in `coordination/handoffs/w1/*.md`)
- **Effective policy.** `Org.ai_data_policy` and `ai-policy.policy` report the *effective* policy: `synthetic_only` whenever consent is invalid (revoked, or `AI_CONSENT_TEXT_VERSION` changed). The stored value is `configured_policy`. `consent.granted_by` is the grantor's email.
- **Additive fields:**
  - `Org.settings`;
  - `Member.role_name`;
  - invitations carry `created_at` and `email_sent`;
  - `user.mfa_enabled`;
  - `POST /invitations/accept` returns `{org, user}`; 404 or 410 means expired or revoked.
- **Additional routes:**
  - `GET /orgs` lists the caller's orgs;
  - `GET /permissions` is the catalog;
  - MFA: `POST /auth/mfa/setup|verify|disable`, and `otp` is accepted at login;
  - `GET /orgs/{org}` lives in `urls_public` (no trailing slash).
- `/activity` returns `{items, next_cursor: null, previous_cursor: null}`. Each item adds `actor_email` and `actor_name`.
- **API keys:**
  - create needs no password re-auth in W1. It **will require `password` from W3**: frontend, add the field now; backend, enforce it in W2.
  - effective permissions = the key's scopes ∩ the creator's current role;
  - management scopes are never grantable.
- **Error codes:**
  - 400: `invalid_credentials`, `mfa_required`, `mfa_invalid`, `reauth_failed` (with `errors:[{field:"password"}]`), `ai_policy_consent_required`, `invitation_invalid`.
  - 401: `api_key_invalid`.
  - 403: `csrf_failed`, `ai_policy_tier_forbidden`, `owner_required`, `invitation_email_mismatch`, `api_key_not_allowed`.
  - 409: `email_taken`, `last_owner`, `already_member`.
  - 429: `login_locked` (with Retry-After), `rate_limited`.
- **Anonymous requests to tenant routes return 401.** Authenticated non-members and other-org resources still return 404.
- **Throttle scopes:** `login`, `signup`, `reauth`, `upload`, `ai`.
- **Governance (Python):** `apps.governance.services.effective_policy(user_id, dataset_id, tables, columns_by_table, *, privilege="query") -> QueryPolicy`, plus `check_dataset_access(user_id, dataset_id, privilege)` and `visible_dataset_ids(user_id, privilege)`. A deny-all row policy is emitted as `RowPredicate(col, "in", ())`. The SQL guard must render `in ()` as `FALSE` and `not_in ()` as `TRUE`.
- **AI:**
  - `GET /orgs/{org}/ai/status` adds `stored_policy`, `consent_valid`, `policy_downgrade_reason`, `token_budget`, `user_budget`, `budget_resets_at`, `models{fast,reasoning}` and `limiter_available`.
  - `GET /orgs/{org}/ai/usage?days=1..90` returns `{range, totals, daily, by_feature, by_status}` (perm `org.settings.edit` or `audit.view`).
  - AI log rows are written through the `durable` DB alias, so they survive a rolled-back request.
- **Datasets:**
  - additive fields `source_kind`, `classification_attested_at`, `owner_id`, `last_error{code,message}` and `last_job_id`;
  - `status` is one of `processing|ready|failed|archived|deleted`;
  - the 202 body adds `status_url`;
  - `POST /datasets` accepts `attest: true` alongside a `synthetic` or `public` classification.
- **Numeric scales:** `quality_score`, `avg_quality` and quality dimensions are **0–100**. `stats.null_pct` is **0–100**. `stats.histogram` is `[{start, end, count}]`.
- **Further dataset and quality routes:**
  - `GET /uploads/{id}`;
  - `GET|POST /datasets/{id}/versions`;
  - `GET|PATCH|DELETE /datasets/{id}/quality-rules/{rule_id}`;
  - `POST /datasets/{id}/quality/evaluate` (202 job).
  - The quality response adds `status`, `summary`, `generated_at` and `duplicates.key_columns`; `key_duplicates` is an integer.
- **Lineage:** node ids are raw UUID strings. Hidden assets collapse into one `{id:"restricted", type:"restricted"}` node, and a root node the caller cannot see returns 404.
- **Isolation fixtures:** every new tenant route with resource kwargs must register an isolation factory in `apps/<app>/tests/isolation.py` (`ISOLATION_FACTORIES`, `KWARG_FACTORIES` or `ISOLATION_EXEMPT`); see `backend/testing/isolation.py`.

## W2: Data Engineering (Data Engineering agent)
| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/orgs/{org}/uploads` | multipart `file` | 201 `{ id, filename, size_bytes, status }` |
| POST | `/orgs/{org}/datasets` | `{ name, upload_id, classification? }` | 202 `{ dataset, job_id, events_url }` (ingest → profile → quality → warehouse) |
| GET | `/orgs/{org}/datasets` | `?search=` | list of `Dataset` |
| GET, PATCH, DELETE | `/orgs/{org}/datasets/{id}` | | `Dataset = { id, name, slug, description, classification, status, row_count, column_count, quality_score, current_version: {id, version, row_count, status, wh_table, created_at}, created_at, updated_at, tags }` |
| POST | `/orgs/{org}/datasets/{id}/classification` | `{ classification, attest: true }` | 200 Dataset (`datasets.classify`) |
| GET | `/orgs/{org}/datasets/{id}/columns` | | `[{ name, source_name, data_type, semantic_type, nullable, is_pii, stats: {null_pct, distinct, min, max, mean, p50, top_values, histogram} }]` |
| GET | `/orgs/{org}/datasets/{id}/preview?limit=100` | | `{ columns: [{name,type,masked}], rows: [[...]] }` |
| GET | `/orgs/{org}/datasets/{id}/quality` | | `{ score, dimensions: {completeness, uniqueness, validity, consistency}, rules: [{id, column, rule_type, severity, passed, failed_count}], duplicates: {exact_rows, key_duplicates, fuzzy_clusters: [...]}}` |
| GET, POST | `/orgs/{org}/datasets/{id}/quality-rules` | | |
| GET, POST | `/orgs/{org}/data-sources` | `{ name, type: "postgresql", config: {host,port,dbname,sslmode,schema}, username, password }` | password write-only |
| POST | `/orgs/{org}/data-sources/{id}/test` · `/introspect` · `/import` | `/import { table, name }` | 202 job |
| GET, POST | `/orgs/{org}/pipelines` · GET, PATCH `/{id}` | `{ name, dag }` | `Pipeline = { id, name, status, dag, schedule, last_run }` |
| POST | `/orgs/{org}/pipelines/{id}/runs` | – | 202 job |
| GET | `/orgs/{org}/pipelines/{id}/runs` · `/pipeline-runs/{run_id}` | | `{ id, status, attempt, started_at, finished_at, duration_ms, rows_in, rows_out, rows_rejected, error_code, error_message, quality_summary, steps: [...] }` |
| GET | `/orgs/{org}/lineage?node_type=dataset&node_id=` | | `{ nodes: [{id,type,label}], edges: [{from,to,label}] }` |
| GET | `/orgs/{org}/data-stats` | | `{ datasets, rows_total, avg_quality, pipelines_active, runs_7d: {succeeded, failed}, storage_bytes }` |

## W3: Conversational BI (Backend `bi` + AI/ML `nl2sql`, `ai` + Data Engineering `sqlguard`, `semantic`)
| Method | Path | Request | Response |
|---|---|---|---|
| GET, POST | `/orgs/{org}/bi/conversations` | `{ dataset_id, title? }` | `{ id, title, dataset_id, created_at, last_message_at }` |
| GET | `/orgs/{org}/bi/conversations/{id}/messages` | | `[Message]` |
| POST | `/orgs/{org}/bi/conversations/{id}/messages` | `{ question }` + `Accept: text/event-stream` | SSE events below; with JSON Accept → 200 `Message` (synchronous) |
| POST | `/orgs/{org}/bi/queries` | `{ dataset_id, sql }` | manual guarded SQL → `{ executed_sql, columns, rows, row_count, truncated }` |
| GET | `/orgs/{org}/bi/query-history` | | `[{ id, occurred_at, user_email, dataset, question, source, verdict, reject_code, executed_sql, row_count, duration_ms }]` |
| POST | `/orgs/{org}/exports` | `{ message_id, format: csv|xlsx }` | file download (stream) |
| GET | `/orgs/{org}/ai/status` | | `{ policy, tier, configured: bool, org_budget: {limit, used}, breaker: "closed|open_minute|open_day", resets_at }` |
| GET | `/orgs/{org}/insights` | | dashboard AI/locally computed insights `[{kind, title, text, dataset_id}]` |

`Message = { id, role: "user"|"assistant", content, status, error_code, created_at, sql: {raw, executed, source}|null, result: {columns, rows, row_count, truncated}|null, chart: {type,x,y,series}|null, insights: [{kind,text}], assumptions: [] }`

**Chat SSE events:**
- `status {stage, source?}`, where `stage` is authorizing → resolving → generating → validating → executing → explaining;
- `sql {raw_sql, executed_sql, source, query_audit_id}`;
- `result {columns, rows, row_count, truncated, chart}`;
- `insight {kind, text}`;
- `token {text}`;
- `done {message_id}`;
- `error {code, detail}`.

## Local runtime: PostgreSQL only, no Docker (binding from W3, Integrator 2026-11-20)

The product must run fully with only a local PostgreSQL that the user creates (`scripts/setup_local.py`: .env holds only POSTGRES_HOST/PORT/USER/PASSWORD + APP_DB_NAME + WAREHOUSE_DB_NAME; internal role passwords are derived from SECRET_KEY by `nexora.dbconfig`), plus
`manage.py runserver` / `uvicorn` and `npm run dev`. Redis, S3/MinIO and pgvector are OPTIONAL accelerators.

**Every screen shows data from the database. There are no hardcoded or mock datasets in the UI.** Demo content comes only from `manage.py seed_demo`, which goes through the real services.

### Settings

All settings below are in `nexora/settings/base.py`.

| Setting | Values | Default |
|---|---|---|
| `REDIS_URL` | URL, or empty | empty |
| `TASK_RUNNER` | `thread` / `celery` / `eager` | `thread` without Redis; tests use `eager` |
| `TASK_RUNNER_THREADS` | integer | |
| `JOB_EVENTS_BACKEND` | `db` / `redis` | |
| `LOCAL_SCHEDULER` | boolean | |
| `STORAGE_BACKEND` | `fs` / `s3` | `fs` without S3 |
| `LOCAL_STORAGE_DIR` | path | |
| `STORAGE_URL_SIGNING_KEY` | secret | |
| `VECTOR_BACKEND` | `pgvector` / `array` / `auto` | |
| `ML_MAX_TRAIN_ROWS` | integer | |
| `ML_MAX_TRAIN_SECONDS` | integer | |

`CACHES` is the Django `DatabaseCache` (table `nexora_cache`) when there is no Redis.

### Backend (`apps.core`): one implementation, used by every app

- **Tasks.** Calling `.delay()` / `.apply_async()` on `TenantTask` (and on a non-tenant `NexoraTask` base for ops tasks) honours `TASK_RUNNER`:
  - `celery`: normal Celery.
  - `eager`: inline.
  - `thread`: submit to a process-wide `ThreadPoolExecutor(TASK_RUNNER_THREADS)`. Each run gets fresh DB connections (`close_old_connections` before and after), runs inside `tenant_context(org_id)` with the actor restored, and honours retries/backoff.

  Callers never branch on the runner.
- **Job events.** `core.jobs.publish()` writes `core.JobEvent` rows when `JOB_EVENTS_BACKEND=db`.
  - Row columns: `org_id`, `job_id`, `seq`, `event`, `data`, `created_at`; RLS-protected and append-only.
  - The SSE view polls the rows. `Last-Event-ID` = `seq`, and the keep-alive protocol is unchanged.
  - Old events are purged by a periodic task.
- **Storage.** `core.storage` keeps its public API and adds a filesystem backend under `LOCAL_STORAGE_DIR/<key>`, with the same key validation.
  - `presigned_get` returns a signed, expiring URL `/api/v1/files/<token>`, served by a streaming view that enforces the expiry and returns an RFC 6266 `Content-Disposition`.
- **Periodic jobs.** `core.scheduler.register_periodic(name, every_seconds, fn)`.
  - With `LOCAL_SCHEDULER=True`, the server process runs one daemon thread. A Postgres advisory lock (`pg_try_advisory_lock`) ensures only one process ticks.
  - With Celery, the same registry feeds beat.
  - Pipelines' schedule dispatcher, retention and job-event purge register here.
- **Health.** `/health` reports `redis: not_configured` and `storage: fs` instead of failing.
- **Demo data.** `manage.py seed_demo [--email --password --org]` creates a demo user and org, then ingests the synthetic `testdata/generated/*.csv` through the real dataset services. Ingestion triggers profiling, quality, the warehouse load and the semantic layer, so the dashboard, BI, C360, P360 and ML have real DB rows.
  - It is idempotent. It prints the login credentials.

### AI/ML (`apps.ai`): vector fallback

- `apps.ai.vectors.EmbeddingField(dim)`: `db_type` is `vector(dim)` when the effective backend is pgvector, otherwise `double precision[]`.
- Migrations create the `vector` extension and HNSW indexes ONLY when the backend is pgvector (RunPython that checks `pg_available_extensions`).
- `apps.ai.vectors.nearest(queryset, field, query_vec, k, *, max_scan=20000)` returns the k nearest by cosine. It uses SQL cosine distance on pgvector, otherwise it loads at most `max_scan` candidate vectors (already org/dataset-filtered) and ranks them with numpy.
- `nl2sql` cache and `documents` chunks MUST use these. W1/W2 migrations that hard-code `pgvector` may be rewritten in place, because no environment has applied them yet.

## W2 as-built additions (binding from W3; full shapes in `coordination/handoffs/w2/*.md`)

The handoff files below are part of this contract. Agents must keep these routes and shapes stable and must only add fields.

### `data_pipelines.md`: data sources

- `GET`/`PATCH`/`DELETE /data-sources/{id}`.
- `GET /data-sources/{id}/imports`.
- `POST /import` also accepts `{columns?, classification?, schema?, mode, cursor_column?, dataset_id?, attest?}`.
- Error code `connector_role_not_readonly`.

### `data_pipelines.md`: pipelines

- `POST /pipelines/validate` returns per-node errors that name the node id.
- `GET /pipelines/{id}/versions`.
- `GET`/`PUT`/`DELETE /pipelines/{id}/schedule`.
- `POST /pipelines/{id}/preview` takes an optional `node_id` and `limit`.
- `GET /pipeline-runs`.
- Run detail, `POST /pipeline-runs/{id}/retry`.

### `ai_ml.md`: documents

- Documents API: upload (202 + job), list/detail/delete, pages, chunks, entities, tables, `search?q=`, `ask`, `summary`.
- `ask` returns `mode: "generative" | "extractive"`, a `reason` when extractive, and citations `[{document_id, page, chunk_id, quote}]`.

### `backend.md`: BI

- The routes are exactly those in the W3 section above.
- SSE clients must ignore `:` keep-alive lines.
- The JSON fallback is the same POST without `Accept: text/event-stream`.
- A persisted assistant `Message` carries the same fields as the SSE events, so a dropped stream can be recovered via `GET /bi/conversations/{id}/messages`.

### `data_engineering.md`: semantic layer

- `GET`/`PATCH` semantic model of a dataset; metrics CRUD.
- `dataset_semantics()` returns `DatasetSemantics` exactly.

### Guard masks

- `mask_null` is rendered `CASE WHEN FALSE THEN col END`, which is type-preserving and never reads the column. QA's oracle must accept this rendering (SECURITY_ARCHITECTURE §5 table).

## W3 additions (binding for W3; Integrator 2026-11-20)

### Dashboards (Backend `bi`)

All routes are under `/orgs/{org}/`. `Dashboard` and `DashboardWidget` are TenantModels with RLS. Permissions: `dashboards.view` and `dashboards.manage`.

| Method | Path | Body | Response / notes |
|---|---|---|---|
| GET | `/dashboards` | – | `[{id, title, description, is_default, widget_count, updated_at}]` |
| POST | `/dashboards` | `{title, description?}` | |
| GET | `/dashboards/{id}` | – | `{..., widgets:[{id, title, kind: chart\|table\|kpi\|text, saved_query_id?, chart, layout:{x,y,w,h}, text?}]}` |
| PATCH | `/dashboards/{id}` | title/description/`is_default`/`layout[]` (bulk positions) | |
| DELETE | `/dashboards/{id}` | – | |
| POST | `/dashboards/{id}/widgets` | `{title, kind, saved_query_id? \| message_id?, chart?, layout, text?}` | `message_id` pins an Ask-AI answer: it creates a `SavedQuery` from the audited `executed_sql`. |
| PATCH | `/dashboards/{id}/widgets/{wid}` | | |
| DELETE | `/dashboards/{id}/widgets/{wid}` | | |
| GET | `/dashboards/{id}/widgets/{wid}/data` | – | `{columns, rows, row_count, truncated, chart, refreshed_at}`. Re-runs the saved query through `sqlguard.validate` + `execute` under the **viewer's** policy. It never serves stored rows. |

Each data refresh is audited (`BiQueryAudit` source `dashboard`).

### Notifications (Backend `core`)

`core.Notification` is a TenantModel. Its fields are `user_id`, `kind`, `title`, `body`, `link`, `read_at` and `created_at`.

| Method | Path | Notes |
|---|---|---|
| GET | `/orgs/{org}/notifications?unread=1` | cursor-paginated |
| POST | `/orgs/{org}/notifications/{id}/read` | |
| POST | `/orgs/{org}/notifications/read-all` | |

Producers call `apps.core.notifications.notify(user_ids, kind, title, body, link)`. Producers:
- pipeline run failed/succeeded (owner);
- import or ingestion finished/failed;
- document processed;
- invitation accepted;
- AI quota exhausted (org admins).

### AI quota UX (AI/ML `ai`)

- `GET /orgs/{org}/ai/status` adds `{quota: {state: ok|low|exhausted, resets_at, used_today, limit_today}, configured: bool, tier}`.
- The BI `error` event for `ai_quota_exhausted` includes `resets_at`.

### Isolation fixtures

Every app with tenant routes that take resource kwargs MUST ship `apps/<app>/tests/isolation.py` factories. `testing.isolation.MissingIsolationFixture` is a failing test.

## W3 as-built additions (binding from W4; full details in `coordination/handoffs/w3/*.md`)

### Dashboards: `is_shared`

- Dashboards and SavedQueries are **private to their owner** unless `is_shared=true`.
- `is_shared` is in the POST/PATCH body and the list/detail payloads, and defaults to `false`.
- A viewer sees their own items plus shared ones. Others get 404.
- Widget data always re-executes under the **viewer's** policy.

### Signed file URLs (`GET /api/v1/files/<token>`)

| Case | Response |
|---|---|
| Valid | 200, streaming download |
| Bad or tampered token, or the object no longer exists | 404 `not_found` (nothing revealed) |
| Expired link | 410 `download_expired` |

Clients treat 410 as "request a fresh link". **This replaces the earlier mission text that said 403.**

### Job-event SSE

- Works under ASGI (uvicorn) and WSGI (`runserver`). Under WSGI the stream is a sync generator, so events are not buffered.
- Following another user's job requires `jobs.view_all`. Otherwise the response is 404.

### More W3 as-built details (binding from W4)

- **List responses.** List endpoints, including `GET /dashboards`, return the standard cursor envelope `{items, next_cursor, previous_cursor}`. Notification lists also return `unread_count`.
- **Notification links** are app-relative UI routes. Examples: `/data/pipelines/<id>/runs/<run_id>`, `/datasets/<id>`, `/settings/ai`.
- **Kinds:** `pipeline.run_failed|run_succeeded`, `import.finished|failed`, `dataset.ingest_finished|ingest_failed`, `document.processed|failed`, `ai.quota_exhausted`, `invitation.accepted`.
- **Data Engineering additive shapes** (`coordination/handoffs/w3/data_engineering.md`):
  - pipeline validate dry run; preview `node_id`;
  - the builder DAG dialect is accepted, but prefer the canonical node shapes;
  - lineage `restricted` and `restricted_count`;
  - data-stats fields `sources`, `runs_7d.total`, `datasets_by_status`;
  - data-source `checks` and `row_estimate`; import takes `{schema, table}`;
  - `/semantic-models` routes;
  - `mask_hash` = salted SHA-256 (SECURITY_ARCHITECTURE §5).
- **AI/ML additive shapes** (`coordination/handoffs/w3/ai_ml.md`):
  - `/ai/status` quota extras: `remaining_today`, `scope`, `rate_limited`, `user`, `limiter_backend`, `embeddings`;
  - documents `search_mode` / `search_mode_message`, `retrieval.mode|reason|pending_embeddings`, `embedding_status` / `embedding_pending`;
  - blocked proposals carry `resets_at`, `retry_after_s` and `quota_scope`.
- **Golden cassettes:** format `nexora.cassette.v1`, stored as `<dir>/<id>.json` or `<dir>/nl2sql/<id>.json`, with `id`, `question` and a required `response` object.
  - Written by `manage.py record_cassettes --out <dir>`, which needs `GEMINI_API_KEY`.
  - Read by QA through `NEXORA_CASSETTES_DIR`.

## W4 additions (binding for W4; Integrator 2026-11-20)

These docs are binding for W4:
- **Routes:** `docs/API_SPECIFICATION.md` Phase 9 (ML), Phase 6 (Customer 360), Phase 7 (Product 360), Phase 5 governance/usage rows, and Phase 11 (platform).
- **ML design:** `docs/ML_ARCHITECTURE.md`.
- **Data model:** `docs/DATABASE_SCHEMA.md`.

Where those docs and this section differ, this section wins.

### Permissions

The Backend seeds these permissions into the system roles, as with the others.

| Area | Permissions |
|---|---|
| ML | `ml.view`, `ml.manage`, `ml.promote`. `ml.predict` is also an **API-key scope**. |
| Customer 360 | `customers.view`, `customers.manage`, `customers.review` |
| Product 360 | `products.view`, `products.manage`, `products.review` |

Grants per role:
- **Owner and Admin:** all of them.
- **Analyst:** `view` + `manage` + `review`, and `ml.promote` only for Admin/Owner.
- **Viewer:** `*.view`.

Platform routes require `User.is_platform_admin` and use the `ops` alias. They **never return tenant data rows**.

### Promotion (W4)

- `POST …/ml/models/{id}/versions/{v}/promote {stage: staging|production|archived}` requires `ml.promote`, is audited, and returns 200 with the version.
- Only one `production` version per model; the previous one becomes `archived`.
- Approval gating arrives with automation in W6. The route stays the same; it will then return 202 plus an approval id when a policy requires approval.

### Cross-app Python services (exact names; parallel agents code against them)

- **`apps.datasets.services.load_current_frame(dataset_id, columns=None) -> polars.DataFrame`** (exists).
  - ML and C360/P360 MUST first compute `apps.governance.services.effective_policy(user_id, dataset_id, tables, columns_by_table, privilege="query")`.
  - Only the user's ALLOWED, unmasked columns may become features or source fields. Masked or denied columns are excluded and listed as `excluded_by_policy`.
- **NEW, Data Engineering: `apps.datasets.services.publish_frame(*, name, frame, user_id, classification="internal", attest=False, dataset_id=None, description="", source="ml") -> DatasetVersion`.**
  - Goes through the normal upload/ingest path, so profiling, quality, warehouse and semantic all run.
  - `dataset_id` adds a new version.
  - Records lineage from the given `source`.
  - Synchronous when `TASK_RUNNER` is `eager`; otherwise returns after enqueueing, with the job id on the version.
- **NEW, AI/ML, for C360 scoring in W5: `apps.ml.services.predict_frame(model_id, frame, *, version=None, explain=False) -> polars.DataFrame`.**
  - Returns the input columns plus `prediction` and, for classifiers, `probability` (plus `explanation` JSON when `explain`).
  - Raises `apps.ml.services.ModelNotReady` when there is no production/staging version.
- **Embeddings for Product 360:** `apps.ai.embeddings.embed_passages(texts)` / `embed_query(text)` / `embedding_available()`, plus `apps.ai.vectors.EmbeddingField` / `nearest(...)`. When the model is unavailable, fall back to full text and say so (`search_mode`).

### Safety rules for ML

- **Artifacts:** stored ONLY through `apps.core.storage` under `orgs/<org_id>/ml/…`, with a sha256 recorded on the version.
- **Loading:** artifacts are loaded only after the sha256 matches and only from the org's own prefix. Never load uploaded or user-supplied pickles.
- **Training:** runs as a `TenantTask` on queue `ml`, honouring `ML_MAX_TRAIN_ROWS` and `ML_MAX_TRAIN_SECONDS` (stratified sample above the cap, recorded on the experiment), with job progress events.
- **Determinism:** a `random_state` is recorded on every experiment.
- **Explanations:** local SHAP (TreeExplainer for tree models, LinearExplainer for linear models, a permutation fallback); bounded to the first 1,000 rows.
- **Gemini** is used only for the optional EDA/metrics narration, through `GeminiService` and the policy gate. Never row values on the free tier.

## W5 (FINAL wave) additions (binding; Integrator 2026-11-21)

These are binding: `docs/API_SPECIFICATION.md` Phase 10 (automation and agents), Phase 8 (document intelligence: rows not yet built), Phase 6 `scoring-runs`.

### Automation step registry (Backend owns `apps.automation`)

- **`apps.automation.registry.register_step(kind: str, handler, *, schema: dict, impact: "low"|"high" = "low")`.**
  - Each app registers its own step kinds in `AppConfig.ready()`, importing the registry lazily.
  - A handler is `handler(ctx: StepContext, config: dict) -> dict` (the JSON output). It runs inside `tenant_context` as the run's actor.
  - Every step re-checks the actor's permissions (no privilege escalation).
- **Built-in step kinds (Backend):** `condition`, `notify`, `approval` (pauses the run until decided), `wait`.
- **Required app step kinds:**
  - Data Engineering: `pipelines.run`, `customers.resolve`, `customers.score`, `products.normalize`;
  - Backend: `bi.saved_query` (runs through the guard under the actor's policy);
  - AI/ML: `ml.batch_predict`, `ml.train`, `documents.process`.
- **Triggers:**
  - `schedule` (cron, via `core.scheduler`);
  - `webhook` (HMAC-SHA256 over the raw body with the trigger secret; reject bodies older than 5 minutes via a timestamp header; no session);
  - `event`: `dataset.ingest_finished`, `pipeline.run_failed`, `pipeline.run_succeeded`, `document.processed`. Producers call `apps.automation.events.emit(kind, payload)`, imported lazily and a no-op when no workflow matches.

### Approvals

- `apps.automation.services.request_approval(*, kind, title, payload, approver_permission, impact, requested_by) -> ApprovalRequest`.
- Decisions for `impact="high"` require password re-auth.
- **ML promotion to `production` now creates an approval** (`approver_permission="ml.promote"`, `impact="high"`) and returns 202 with `approval_id`. The version is promoted when the approval is granted, via the hook `apps.ml.services.apply_promotion(approval)`.
- Staging promotion stays immediate (200).

### Agents (`/agents`)

- An agent config is a tools allowlist (registered step kinds only), scopes and approval rules.
- Agent planning MAY use Gemini only through `GeminiService` and the policy gate, with schema and step metadata only (never row values on the free tier).
- Every tool call is an audited step run. `impact="high"` tools always require approval.

### Errors

- Problem responses MUST be served with `Content-Type: application/problem+json`. Set `response.content_type`; DRF otherwise overwrites the header (W4 defect).
