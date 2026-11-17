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
