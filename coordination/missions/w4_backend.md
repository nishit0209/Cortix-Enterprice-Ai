You own `backend/apps/{core,accounts,orgs,governance,bi,platform_admin,automation}/**`.

## A. Permissions

Seed every permission in CONTRACT "W4 additions" (ML, customers, products) into the system roles, with the grants specified there, through a data migration (idempotent, like the existing permission seeding). ML / C360 / P360 agents code against these names IN PARALLEL.

Also make sure API keys can carry the scope `ml.predict`. ApiKey scope validation must accept it, and a key may only grant scopes its creator holds.

## B. Governance and usage APIs

Per `docs/API_SPECIFICATION.md` Phase 5, verify against the spec that every row exists and works, and fill the gaps:
- dataset ACLs, column policies, row policies, user attributes, policy preview;
- retention-policies CRUD (`governance.retention.edit`) PLUS a real periodic retention job via `apps.core.scheduler.register_periodic("governance.retention", 3600, fn)`. It deletes or archives data older than the policy for supported targets (audit events beyond legal hold excluded; bi query audit? no — append-only tables are NEVER deleted by retention; document which targets are supported), dry-run first, audited;
- access-history;
- `GET /usage?from=&to=&metric=` (`org.view`): REAL aggregates from the DB, including queries per day/user, datasets, storage bytes (sum of upload sizes + warehouse table sizes via the warehouse services if exposed), active users, AI requests/tokens (from `apps.ai` usage tables through its services), pipeline runs, documents.

All of it is tenant-scoped and RLS-safe.

## C. Platform super-admin (Phase 11)

Build `apps/platform_admin` API under `/api/v1/platform/…` for `User.is_platform_admin` only (everyone else gets 404). It uses the `ops` DB alias (BYPASSRLS) and returns counts and status only. NEVER tenant data rows.

Endpoints:
- `GET orgs` / `orgs/{id}`: members count, datasets count, storage, AI usage, status, created, plan;
- `POST suspend` / `reactivate`: a suspended org's members get 403 `org_suspended` on tenant routes. Enforce it in the tenancy base view. Audited in a new append-only PlatformAuditEvent;
- `GET health`: DB, cache table, storage backend, task runner, scheduler last tick, Gemini configured/reachable via `apps.ai` status, embeddings status;
- `GET jobs`: thread-runner/Celery queue status from core job tables, recent failures;
- `GET ai/usage` and `PUT ai/model-quotas/{model}` (through `apps.ai` services);
- `GET logs`: recent structured app log records, kept in a bounded DB-backed ring table written by a logging handler for WARNING+, level/component filter.

Add a management command `create_platform_admin --email` that sets the flag on an existing user. The final user guide will reference it.

## D. Wave-3 leftovers

1. **fs storage on Windows.** `_fs_write` `os.replace` raises PermissionError [WinError 5] while a download holds the file open. Retry with a short backoff, then write to a fresh key, or document it. Failing test: `backend/tests/security/test_fs_storage.py::test_overwrite_succeeds_while_a_download_holds_the_file_open`.
2. **AI/ML request.**
   - `apps/bi/services.py` `_insights()` must call `ports.compute_insights(column_dicts(rs.columns), rows, sql=<executed SQL>, truncated=rs.truncated)`.
   - `apps/bi/ports.compute_insights(columns, rows, **kwargs)` must forward them to `apps.nl2sql.services.compute_insights(columns, rows, *, complete=None, sql=None, truncated=None)`.
3. **seed_demo.** It must not re-hash an unchanged `--password` (skip `set_password` when `check_password` holds). Dataset names contain the CSV stem. The default dashboard has a total-revenue KPI and revenue-by-region widgets (QA request).
4. **Saved-query permissions.** Saved-query writes are gated on `dashboards.create` while pinning uses `dashboards.manage`. Make them consistent (document the rule in your handoff).
5. **Docs.** The `apps/core/notifications.py` docstring example must use UI routes (`/data/pipelines/<id>/runs/<run_id>`).

## E. Tests

Write:
- unit tests where possible;
- DB/API tests for: permissions seeding, governance/usage, platform endpoints (platform admin vs normal user 404, suspended org 403, no tenant rows in payloads), retention dry-run/apply, ApiKey `ml.predict` scope;
- isolation factories for every new resource route.
