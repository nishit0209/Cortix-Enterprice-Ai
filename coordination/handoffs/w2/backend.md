# Handoff — backend — wave 2

Branch: `agent/backend/w2`   Base: `main` (all wave-1 work merged)   Contract: CONTRACT.md "W3: Conversational BI" implemented as binding; W1 as-built addenda honoured.

Note: this run was interrupted once by a usage limit and resumed. No existing commits were rewritten.

## Done

### A. QA defects (one commit each)
1. `f95112f` core/tenancy/operations.py: `MakeAppendOnly` / `EnableTenantRLS` SQL runs verbatim (`params=None`), so a `%` in `RAISE ... 'table % is append-only'` no longer breaks `migrate` on a fresh DB. I checked every RunSQL/execute in the backend migrations for the same bug.
2. `efbfe28` core/tenancy/context.py: `tenant_context()` saves `current_setting(TENANT_GUC, true)` on entry and restores it on exit. When the atomic block is rolling back, the savepoint rollback reverts it instead.
3. `da3d654` core/tenancy/tasks.py: `TenantTask.__call__` resets `current_actor` (and the org/tenant vars) with ContextVar tokens in `finally`.
4. `6c52b46` and `5f3ec64` core/tenancy/views.py: `finalize_response` calls `transaction.set_rollback(True)` only when the mixin entered its own atomic block (`_tenant_atomic_entered`). Views can opt out with `rollback_on_error = False`. bi uses this so the audit rows of rejected queries survive a 4xx.
5. and 6. `8cc5e39` core/storage.py: `_check_key` / `org_key` reject `.`, `..`, empty segments, backslashes and control characters (including NUL, CR and LF). `_check_key` also requires the strict `orgs/<active org>/` prefix, so an absolute or foreign key is rejected; `org_key` strips leading `/` so a part always stays under the org prefix. `presigned_get(filename=)` uses the new `apps.core.http.content_disposition`, an RFC 6266 / 5987 header value: a quoted ASCII fallback that keeps only `[A-Za-z0-9 ._()-]`, plus `filename*=UTF-8''<pct-encoded>`. The companion helper `safe_filename` strips path separators and control characters.
   - QA's strict-xfail repros `tests/security/test_storage_keys.py::{test_check_key_rejects_path_traversal_segments,test_presigned_filename_is_sanitised}` now report XPASS(strict). QA needs to remove those markers. The GUC, actor and finalize repros in `tests/isolation/*` need the DB, so I could not run them.

### B. Password re-auth (`c7485e6`)
- `POST /orgs/{org}/api-keys` takes `{name, scopes, password}`. A wrong or missing password returns `400 reauth_failed` with `errors:[{field:"password", code:"reauth_failed", message}]`. This lives in a shared helper, `apps.core.auth.require_password` / `core.exceptions.ReauthFailed`.
- The AI-consent wrong password (`POST /ai-policy/consents`) now uses the same helper and returns the same `errors:[{field:"password"}]` shape.

### C. Conversational BI: apps/bi (`24f4739`, tests `40918bc`)
- Models (all TenantModel + RLS):
  - `Conversation(user_id, dataset_id, title, last_message_at)`
  - `Message(role, content, status pending|ok|error|clarification|blocked|cancelled, error_code, sql_raw, sql_executed, sql_source, result (capped rows and bytes), chart, insights, assumptions, query_audit_id, feedback)`
  - `BiQueryAudit`: append-only via `MakeAppendOnly`, with every field the mission lists
  - `SavedQuery`
  - Migration: `bi/0001_initial.py`.
- `apps/bi/ports.py` is the only place bi calls other agents' apps. It resolves at call time so tests can monkeypatch it. It uses:
  - `semantic.services.dataset_semantics`
  - `sqlguard.services.validate/execute`
  - `nl2sql.services.propose/repair/remember/store_verified`
  - `nl2sql.insights.compute_insights/narrate_template`
  - `governance.services.*`
- `apps/bi/services.ask(turn)` is a generator of `(event, data)` tuples, used by both the SSE path and the JSON path. The pipeline:
  1. authorize: `bi.chat` plus `check_dataset_access(..., "query")`. An unknown or invisible dataset returns 404.
  2. `dataset_semantics`.
  3. `effective_policy(privilege="query")`.
  4. `QuestionContext`, with the columns filtered by the policy and value-free history (the last 5 turns: question, SQL, column names only).
  5. `propose`. A `blocked_reason` becomes an error event with that code and a friendly detail. A clarification becomes an assistant message with status `clarification`.
  6. `validate`. A code in `REPAIRABLE_REJECT_CODES` gets exactly one `repair` and a re-validate. A repairable execution error is also repaired once. Every rejection is audited with `verdict=rejected` and emitted as `error{code: reject_code}`.
  7. `execute` with `QueryTarget(kind="warehouse")`.
  8. Chart: the proposal's `chart_hint` when it matches the result, otherwise a heuristic (line / bar / pie for 6 or fewer share-like slices / kpi / table). Masked numeric columns are never plotted. Insights come from local `compute_insights` plus `narrate_template` (no LLM).
  9. A `BiQueryAudit` row, a governance `AccessHistory` row, then `remember` (skipped on cache hits; it runs in its own savepoint, so a cache failure never loses the answer).
  - Every DB step runs in a short `tenant_context` (`_step`), and no transaction spans a `yield`. A client disconnect finalises the message as `cancelled`. An unexpected exception becomes `internal_error`.
- Events follow the CONTRACT exactly: `status{stage}`, `sql{raw_sql, executed_sql, source, query_audit_id}`, `result{columns, rows, row_count, truncated, chart}`, `insight{kind, text}`, `done{message_id}`, `error{code, detail}`.
- SSE (`apps/bi/streaming.py`):
  - With `Accept: text/event-stream`, `POST /bi/conversations/{id}/messages` streams. Under ASGI the pipeline runs in one worker thread, sending a `: keep-alive` comment every 10 s. Under WSGI or in tests it iterates directly.
  - Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`.
  - Errors raised before streaming starts (403/404/400) are normal JSON problems.
  - Without that Accept header, the same endpoint returns the finished `Message` synchronously.
- Endpoints:
  - `/bi/conversations`: list and create. Detail routes support get, patch title and delete, and are owner-only (anyone else gets 404).
  - `GET/POST /bi/conversations/{id}/messages`: cursor-paginated, `ai` throttle scope on POST.
  - `POST /bi/messages/{id}/feedback {value: up|down}`: `up` calls `store_verified`, but only when the SQL is still valid under the user's current policy. Otherwise it is stored without being verified.
  - `POST /bi/queries`: manual SQL, perm `bi.sql`, `source=manual`. It goes through the same guard, execute and audit path; a rejection returns 422 and stays audited.
  - `GET /bi/query-history`: `audit.view` sees all users, everyone else sees their own.
  - `/saved-queries` CRUD plus `POST /saved-queries/{id}/run`: the SQL is guarded on save and on run, with sharing.
  - `POST /exports {message_id, format csv|xlsx}`, perm `exports.create`: re-executes the audited `executed_sql` through the guard under the current `export` privilege policy, so it never trusts stored rows. The output is formula-injection escaped (cells starting with `= + - @ \t \r` get a `'` prefix; numbers stay numbers). The export is audited, and a rejection on re-execution is audited too.
  - `GET /insights`: the latest insights from the caller's recent assistant messages, limited to datasets the caller can still see.
- Isolation factories are in `apps/bi/tests/isolation.py` for every resource route.

### D. governance AccessHistory (`0698b5b`)
- `AccessHistory` is append-only with RLS (migration `governance/0002_access_history.py`), written by bi through `governance.services.record_access(...)`.
- `GET /access-history`: `audit.view` sees everything, everyone else sees their own.
- Isolation factories are in `apps/governance/tests/isolation.py` and `apps/orgs/tests/isolation.py`.

## Tests (REAL counts, this run)
Infra (Postgres :5433, Redis :6380, MinIO :9000) was DOWN: the TCP probe failed and psycopg reported `connection timeout expired`. All DB tests were written but could not be executed. Every error below is `django.db.utils.OperationalError: connection timeout expired` at DB setup, which I checked by grouping the error causes. There were no assertion failures.

| suite | passed | failed | errors (not executed: infra unavailable) |
|---|---|---|---|
| apps/bi | 44 | 0 | 29 |
| apps/core | 10 | 0 | 7 |
| apps/accounts | 13 | 0 | 50 |
| apps/orgs | 0 | 0 | 53 |
| apps/governance | 44 | 0 | 19 |
| apps/platform_admin, apps/automation | 0 | 0 | 0 (no tests) |
| **total** | **111** | **0** | **158** |

The non-DB bi tests that passed cover:
- the pipeline: event order and payloads, value-free policy-filtered context, repair exactly once, terminal second rejection, security rejections never repaired, policy block, clarification, invisible or unknown dataset, internal error, disconnect leading to `cancelled`
- SSE framing, keep-alives, the one-thread pipeline, disconnect handling, response headers
- the chart heuristic, result capping, and CSV/XLSX formula escaping

QA suite `backend/tests/**` (not owned by me; non-DB results):
- `test_storage_keys` traversal and filename tests are XPASS(strict), so my fixes work and QA must drop the markers.
- `test_tenant_unique_constraints_include_org` still fails on `ApiKey.prefix` and `Invitation.token_hash`. This is known and by design (see FEATURE_STATUS open issue 1); QA needs to add an allowlist.
- All the others need the DB.

Checks:
- `scripts/check_conventions.py`: OK
- `scripts/check_ownership.py --agent backend --base main`: OK
- `ruff check` on the owned apps: all passed
- `manage.py check`: no issues
- `makemigrations --check --dry-run`: No changes detected

## Requests
- Integrator: once infra is up, run the owned DB suites: `$env:PYTEST_DB_SUFFIX="backend"; pytest apps/bi apps/core apps/accounts apps/orgs apps/governance --create-db`.
- Data Engineering: bi codes against exactly these, used through `apps/bi/ports.py`:
  - `apps.semantic.services.dataset_semantics(dataset_id) -> nexora.contracts.DatasetSemantics`
  - `apps.sqlguard.services.validate(sql, policy) -> GuardResult`
  - `apps.sqlguard.services.execute(guard, policy, QueryTarget) -> ResultSet`
  - Any signature drift breaks the chat at runtime; the tests use fakes.
- AI/ML: bi calls these with the w1-handoff signatures:
  - `nl2sql.services.propose(question, ctx)`
  - `repair(question, bad_sql, reject_code, ctx)`
  - `remember(question, sql, ctx, *, source, explanation, assumptions, chart_hint, dataset_version_id)`
  - `store_verified(...)`
  - `nl2sql.insights.compute_insights(...)` and `narrate_template(...)`
- Frontend: SSE consumers must ignore `:` comment lines (keep-alives). The JSON fallback is the same endpoint without `Accept: text/event-stream`.

## Open issues
- None of the DB tests in this wave were executed (infra down), including the bi API tests, the RLS on the new bi/governance tables, the append-only triggers, and QA's GUC, actor and finalize repros. They are written and complete.
- The SSE keep-alive and thread path is only exercised under ASGI, through the unit tests in `test_streaming.py`; I have not run it against a real uvicorn and nginx deployment.

## Defects for other agents
- QA: remove the strict-xfail markers on `tests/security/test_storage_keys.py` (they now XPASS). Also allowlist `accounts.ApiKey.prefix` and `orgs.Invitation.token_hash` in `test_tenant_unique_constraints_include_org`: these are server-generated random lookup keys resolved before the tenant is known, through SECURITY DEFINER functions.
