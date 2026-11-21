# W4 handoff: backend

## Done
- **Permissions:** ML/C360/P360 permissions are seeded into the system roles (orgs 0004, idempotent). API keys accept the `ml.predict` scope, and a key can only grant scopes its creator holds.
- **Governance:**
  - Retention policies: CRUD, then a dry run, then enforce, with legal hold and audit. There is an hourly `governance.retention` job.
    - Supported targets: core notifications, and BI conversations (delete or archive).
    - Append-only tables are never deleted.
  - Policy preview takes a sample under the intersected policy.
  - `GET /usage` returns real aggregates.
- **Platform super-admin `/api/v1/platform/...`:** orgs list and detail, suspend and reactivate, health, jobs, AI usage and model quotas, and logs (a DB ring buffer for WARNING and above).
  - Only `is_platform_admin` users can reach it. Everyone else gets 404.
  - It uses the `ops` alias only. The `platform_admin_*` tables are REVOKED from `nexora_app`.
  - PlatformAuditEvent is append-only.
- **`manage.py create_platform_admin --email X [--revoke]`**
- **Suspended org:** members get 403 `org_suspended`. Valid API keys of that org now also get 403 `org_suspended`, not 401 (accounts migration 0003: the key lookup returns `org_status`).
- **Fixes found by DB runs:**
  - The governance column-policy and row-policy create returned **500** (dataset_id NULL). The MRO let the DRF mixins shadow `DatasetScopedViewSet.perform_*`, so update and delete also skipped the audit. Fixed, with a regression test.
  - The governance API tests popped the real dataset-existence checker from the registry, which polluted global state. The stub is now per test.
  - The BI retention `archive` matched no rows because of NULL logic in `exclude(...)`. Fixed.
  - BI message feedback now returns 404 for a foreign id before it validates the body (the IDOR matrix expected this).
- **W3 leftovers:**
  - fs storage on Windows: replace/share-delete.
  - BI insights now get the executed SQL and the truncated flag.
  - seed_demo: the password hash stays when unchanged, and dataset names keep the CSV stem.
  - Saved-query writes now need `dashboards.manage`, the same permission pinning uses. Viewing needs `dashboards.view`.
  - The notifications docstring now uses UI routes.

## Tests (real Postgres, PYTEST_DB_SUFFIX=backend)
- Owned suites (core, accounts, orgs, governance, bi, platform_admin, automation): **554 passed, 1 failed, 1 skipped**.
  - The one failure is `bi/test_w4_usage_and_preview.py::test_usage_report_counts_real_rows`. It is caused by the ai_ml `timeutil` defect below: the `ai` metric is reported as unavailable.
- Also fixed: the test-DB flush wiped the permission catalog. orgs now re-seeds it on post_migrate.
- Static checks pass: conventions, ownership, ruff, manage.py check, and makemigrations --check.

## Defects for others
- **ai_ml:** `GET /orgs/{org}/ai/usage` returns 500. `apps/ai/timeutil._as_dt` calls `float()` on a `datetime.date`. This also makes `/usage` log "metric ai failed".
- **qa_security:**
  - Platform tests need `django_db(transaction=True, databases="__all__")`, because ops is a separate connection.
  - PlatformAuditEvent is readable only via `using("ops")`, so `_strict_tables` must skip `platform_admin_*`.
  - `Organization.objects.get(...)` outside a tenant context is empty under RLS. Use the ops alias or `tenant_context`.
  - Tests that are stale against the as-built behaviour:
    - SSE viewer access needs `jobs.view_all`.
    - Signed URLs return 404/410.
    - API-key create needs at least one scope and a password re-auth.
    - `/health` checks changed under the local runtime.
    - The uploads matrix POST must be multipart.

## Open issues
- None known in owned code.
