# Handoff: data_engineering, wave 2

Branch: `agent/data_engineering/w2`   Base: `main` (`929dcf3`)   Contracts: `backend/nexora/contracts.py` (W2: DatasetSemantics, GUARD_REJECT_CODES, PolicyService)

This run resumed after an interruption. Commit 7f1f18f was already there. The uncommitted work was reviewed, finished and committed as 379dc50, 697271c and 8726a40.

## Done

### C. SQL guard + executor (`backend/apps/sqlguard/`, `apps/warehouse/services.py`)
- `services.validate(sql, policy) -> GuardResult` follows SECURITY_ARCHITECTURE §5. It uses sqlglot with the postgres dialect.
  - Input limits: a 10 KB size cap and a 1500 AST-node cap. Exactly one statement, which must be SELECT, WITH...SELECT or a set operation.
  - It rejects:
    - Command nodes, INTO, FOR UPDATE/SHARE, RECURSIVE, comments and dollar-quoting;
    - system columns and pg_catalog/information_schema objects;
    - schema-qualified and 3-part names;
    - reg* casts;
    - set-returning functions and table functions in FROM, and LATERAL to functions;
    - NATURAL JOIN.
  - Functions are checked against an allowlist (`allowlist.py`); anything else is `forbidden_function`.
  - Every table is resolved against `policy.tables` in every scope: CTEs, subqueries and set-op branches.
  - Columns are checked with `qualify(expand_stars, validate_qualify_columns)` against a schema that holds only the allowed columns. A denied column anywhere is `column_not_allowed`. That includes `t.*`, `row_to_json(t)` and a bare table reference.
  - Secure-source substitution (`rewrite.py`): every base table becomes `(SELECT <allowed cols / mask exprs> FROM "schema"."table" WHERE <row predicates>) AS alias`. Predicates use bound parameters. Empty `in` becomes FALSE and empty `not_in` becomes TRUE. Masks: `mask_null`, `mask_hash`, `mask_partial`.
  - The query is wrapped as `SELECT * FROM (...) _q LIMIT row_cap`, then regenerated and re-parsed as a round-trip check. An independent output verifier (`verify.py`) checks the result again.
  - Only codes from `GUARD_REJECT_CODES` are returned.
- `services.execute(result, policy, target) -> ResultSet`:
  - It refuses a target whose org differs from the policy's org.
  - It runs `BEGIN READ ONLY` as the org's reader role, with statement/lock timeouts, work_mem and `search_path=pg_catalog`.
  - It runs `EXPLAIN (FORMAT JSON)` first. Every plan relation must be one of the policy's physical tables (`plan_relation_not_allowed`), and the total cost must be within `NL2SQL_MAX_COST` (`cost_exceeded`).
  - It then reads through a named cursor with fetchmany and row/byte caps (setting `truncated`), and maps errors to `timeout` or `execution_error`.
  - `kind="external"` returns a clean `execution_error` ("External targets arrive with the PostgreSQL connector.").
- `services.validate_expression(expr, columns)` validates metric expressions; the semantic layer uses it.
- `tasks.py` adds `run_query_wh` and `run_query_ext` (TenantTask; the queues are already routed in `nexora/celery.py`) and the `run_guarded_query(...)` body. The flow is dataset_schema → `effective_policy(user, dataset, tables, columns_by_table)` → validate → execute. It never raises for guard or query errors; it returns `{"ok": False, "reject_code", "message"}`.

### A. Registrations (`apps/datasets/apps.py`)
- `register_asset_classifier("dataset", dataset_classification)`, `register_asset_exists("dataset", dataset_exists)` (deleted datasets count as not existing), and `register_dataset_schema_provider(dataset_schema)` (logical → physical tables and all columns of the current ready version).
- `apps/datasets/naming.py` gives the logical table name = dataset slug as a safe snake_case identifier. It is never a reserved word or `pg_*`, and is at most 63 characters.

### B. Semantic layer (`apps/semantic/`)
- Models: `SemanticModel`, `SemanticTable`, `SemanticColumn` and `MetricDefinition`. All are TenantModels with RLS (migration 0001 with EnableTenantRLS).
- Auto-generation: datasets sends the `dataset_version_published` signal after a successful ingestion that makes a version current. Semantic regenerates the model in a savepoint; a failure is recorded on the model and never fails the ingestion. The model is also generated lazily on first access (`ensure_model`).
- Generated content:
  - column roles (dimension, measure, time, id, pii) from types and semantic types;
  - descriptions;
  - synonyms (name splitting plus business synonyms such as revenue~sales~turnover and region~zone);
  - default metrics: row count, plus sum and avg of each measure.
- Refresh keeps user edits (`edited_fields`), drops columns that disappeared, regenerates default metrics, and re-validates custom metrics (marked `invalid` when they no longer apply). Detected PII stays `pii`.
- `services.dataset_semantics(dataset_id) -> DatasetSemantics` matches the exact contract: logical → physical via `warehouse.physical_table`, ALL columns, `columns_meta` dicts, active and valid metrics, and `schema_hash`.
- Endpoints (reads need `datasets.view` plus dataset ACL `read`; writes need `semantic.manage` plus ACL `admin`):
  - `GET/PATCH /datasets/{id}/semantic-model`
  - `POST /datasets/{id}/semantic-model/refresh` (`{reset}`)
  - `GET/POST /datasets/{id}/metrics`
  - `GET/PATCH/DELETE /datasets/{id}/metrics/{metric_id}`. Deleting a default metric deactivates it.
- Isolation factories are added for the `dataset-metric`, `dataset-quality-rule` and datasets routes (`apps/*/tests/isolation.py`).

### D. Datasets
- `preview(dataset, user_id=...)` is now governed. It calls `effective_policy` with privilege `read`: denied columns are omitted, masks are applied at the source, and row policies are applied. It runs through `sqlguard.validate` and `sqlguard.execute` as the org reader role.
- `DATASET_MAX_COLUMNS` and `DATASET_MAX_ROWS` are read from the real settings.
- The `create_upload`, `create_dataset` and `add_version` signatures are unchanged. `publish_frame` was not added because it is not trivial; the pipelines agent uses the upload path.

## Tests (real counts, 2026-11-20)
Postgres at localhost:5433 was unreachable (connection timeout), so the DB tests did not run.
- Owned apps (`pytest apps/sqlguard apps/semantic apps/datasets apps/quality apps/warehouse`): 579 collected. **505 passed, 0 failed, 74 errored at setup** with `OperationalError: connection timeout` (the infra was unavailable, so these were not executed).
  - The DB tests that did not run: sqlguard/test_execute_db 15, semantic/test_api 10, datasets/test_api 21, datasets/test_governed_preview 7, quality/test_api 5, warehouse/test_warehouse 16.
  - The tests that passed: sqlguard test_validate 196, test_rewrite 50, test_execute_unit 27; semantic test_generation 29; datasets 129; quality test_engine 57; warehouse test_naming 17.
- QA corpus (`pytest tests/security/sql`): 135 collected. **117 passed, 16 failed, 2 skipped** (the 2 skips are `stage: execute` cases).
  - Every failure is a reject-code naming mismatch. In each case the query **is rejected** with a code from the contract's `GUARD_REJECT_CODES`.
  - The corpus expects codes that are not in the contract (see the defects section below).
- Checks: ruff OK; `manage.py check` OK; `makemigrations --check` reports "No changes detected" (the history-consistency check was skipped because the DB was down); check_conventions OK; check_ownership OK.

## Requests
- None for shared files. The Celery routes for `run_query_wh` and `run_query_ext` already exist.

## Open issues
- The DB tests (74) need the infra (Postgres, Redis, MinIO) to be run.
- The 16 corpus failures depend on QA aligning the code names (see below).
- `publish_frame` has not been added.

## Defects for other agents
- **qa_security**: `backend/tests/security/sql/corpus.yaml` expects reject codes that are not in `nexora.contracts.GUARD_REJECT_CODES`. The guard is required to return only contract codes. Mapping to use:
  - `forbidden_construct` → `forbidden_clause`, for comments, dollar-quoting and similar (smuggle-005/006/021/022/023, plus `recursive_cte` for dos-001);
  - `system_catalog` → `system_object` (xtenant-005/006/007/008/011/013/016, inject-003);
  - `forbidden_type` → `forbidden_cast` (func-009);
  - `cte_shadowing` → `table_not_allowed` (rowpol-006);
  - also drop `command_not_allowed` from the corpus.
- **backend / governance owners**: isolation factories are missing for `member-detail`, `invitation-detail`, `api-key-detail`, `user-attributes`, `dataset-acl-detail`, `column-policy-detail` and `row-policy-detail`. `testing.isolation.factory_for` raises MissingIsolationFixture for these routes, so the isolation matrix will fail.
