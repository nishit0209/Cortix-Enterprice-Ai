# W4 handoff: qa_security

## Done
- W4 suites (ML artifact/prediction security, ML correctness oracle, platform/governance/usage/retention, C360/P360 ER quality, e2e flows), W3 leftovers (signed URL 404/410, golden g39/g45, fake_redis/s3 fixtures, SSE jobs.view_all, is_shared).
- First run against the real PostgreSQL: fixed test bugs (connector SSRF resolver + bytes keys, documents/ask JSON body, pipeline DAG canonical shape, matrix probes for multipart/body-validating routes, check_policy kw-only args, readyz as-built checks, API-key reauth body, audit TRUNCATE is a test-DB-only grant, ML static self-test vs conventions scan).

## Tests (real DB, PYTEST_DB_SUFFIX=qa_security)
- backend tests/{isolation,security,ai_eval,integration} + testing: 1762 passed, 78 skipped, 3 failed (all app defects below), 1 xfailed (defect below).
- e2e: tsc --noEmit OK; Playwright not run (app not running, E2E_SKIP_IF_DOWN).
- conventions OK, ownership OK, ruff OK, manage.py check OK, makemigrations --check: no changes.

## Defects for other agents
1. backend: apps/governance/views.py:94,108,115 - DRF mixins precede DatasetScopedViewSet in the bases, so CreateModelMixin.perform_create shadows DatasetScopedViewSet.perform_create (views.py:70): POST datasets/{id}/row-policies (and column-policies/acl) -> 500 IntegrityError dataset_id NULL, no audit. Fails tests/integration/test_dashboard_widget_policy.py. Fix: put DatasetScopedViewSet first or override perform_* in each viewset.
2. ai_ml: apps/ai/services.py:294 - next_pacific_midnight(end) gets a date; apps/ai/timeutil.py:18 float(date) -> TypeError: GET /orgs/{org}/ai/usage always 500. Fails test_endpoint_matrix[ai-usage].
3. backend: apps/core/storage.py:167 - os.replace onto a file held open by a download fails on Windows (WinError 5) -> StorageError. Fails tests/security/test_fs_storage.py::test_overwrite_succeeds_while_a_download_holds_the_file_open. Fix: retry/open share-delete.
4. data_engineering: apps/pipelines/steps.py:23 NewName has no identifier pattern; rename/derive publish columns like "x; DROP TABLE y", breaking the ingest invariant (apps/datasets/ingest/columns.py). xfail(strict) in tests/security/test_pipeline_dag_injection.py (rename-to-sql).

## Requests
- backend (hardening): add a BEFORE TRUNCATE statement trigger on append-only tables (row trigger does not fire on TRUNCATE).
