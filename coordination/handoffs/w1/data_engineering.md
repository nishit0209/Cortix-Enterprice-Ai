# Handoff — data_engineering — wave 1

Branch: `agent/data_engineering/w1`   Base: `fd484d9`   Contract version: `contracts/api/CONTRACT.md` (W2 Data Engineering section)

## Done

### A. Warehouse (`backend/apps/warehouse/`)
- `naming.py`: schema `<WH_SCHEMA_PREFIX><short_id>`. Reader role `wh_q_<short_id>` in production and `wh_q_<prefix><short_id>` in tests (prefix-aware). Dataset tables are `ds_<12 hex of dataset uuid>_v<n>`. Every identifier is checked against `^[a-z_][a-z0-9_]{0,62}$`.
- `services.provision(org_id)` is idempotent and takes an advisory lock. It does:
  - `CREATE SCHEMA IF NOT EXISTS` and `REVOKE ALL ... FROM PUBLIC`;
  - creates the `NOLOGIN NOINHERIT` reader role;
  - `GRANT USAGE` on the schema, plus `ALTER DEFAULT PRIVILEGES ... GRANT SELECT` and `GRANT SELECT ON ALL TABLES` (self-healing);
  - `GRANT <role> TO <executor login>`, where the login name is parsed from `WH_DATABASE_URL_EXECUTOR`;
  - then upserts `WhOrgBinding` (TenantModel + RLS).
- Provisioning on org creation: `AppConfig.ready` connects `on_org_created` to `apps.core.signals.org_created`.
  - Normally it runs the Celery task `apps.warehouse.tasks.provision_org` (queue `ingest`, retries with backoff).
  - In eager mode it runs inline, once.
  - A failure never breaks org creation, because `ensure_provisioned()` provisions lazily on first use.
- Other services:
  - `reader_role(org_id)`, `org_schema(org_id)`, `physical_table(dataset_version)` → `"schema.table"`, `drop_table`, `drop_org_objects(short_id)` (for the purge job).
  - `load_table(org_id, table, [ColumnSpec], polars_df)`: strict cast to the PG types, then `CREATE TABLE`, then `COPY ... FROM STDIN (FORMAT csv)` in 50k-row slices. Strings are always quoted, so `''` and NULL stay distinct. Then `GRANT SELECT` to the reader, a table comment, `ANALYZE` and `pg_total_relation_size`. It refuses an existing table (`warehouse_table_exists`): versions are immutable.
  - `read_table(org_id, table, columns, limit=None)`: COPY TO STDOUT into Polars, with the types restored. Used for internal system reads such as quality re-evaluation.
  - `execute_readonly_sql(org_id, sql, params, timeout_s, row_cap, byte_cap)` → `nexora.contracts.ResultSet`. It runs: connect as `WH_DATABASE_URL_EXECUTOR` → `BEGIN READ ONLY` → `SET LOCAL ROLE <reader>` → `set_config(statement_timeout, lock_timeout, idle_in_transaction_session_timeout, search_path=<org schema>, TimeZone=UTC)` (all transaction-local) → `current_user` check → server-side cursor (`DECLARE`, so exactly one SELECT statement under the extended protocol) → row/byte caps with a `truncated` flag and a wall-clock budget across FETCHes → `ROLLBACK`.
  - The executor refuses to run without a tenant context or for another org (`WarehouseTenantMismatch`).
  - Defense in depth: a denylist regex for `set_config`, `SET/RESET ROLE`, `pg_read_file`, `lo_*` and `dblink`.
  - Errors map to `query_timeout` (504), `query_permission_denied` (403), `query_failed` (422) and `warehouse_unavailable` (503).
  - `preview_rows(...)` is built on this executor.

### B. Datasets (`backend/apps/datasets/`)
- Models (all TenantModel + `EnableTenantRLS`):
  - `Upload`.
  - `Dataset`: classification `internal` by default. A CHECK constraint requires `classification_attested_by/at` for `synthetic`/`public`. It also has `last_error`, `last_job_id`, soft delete, and `tags text[]`.
  - `DatasetVersion`.
  - `DatasetColumn`.
- Upload API `POST /uploads` (multipart `file`, `datasets.create`):
  - checks Content-Length early, then the file size against `MAX_UPLOAD_BYTES` (413 `upload_too_large`);
  - checks the extension allow-list (`.csv .tsv .txt .xlsx .xls .json .ndjson`) and magic bytes / binary sniffing (422 `upload_type_not_allowed` / `upload_content_mismatch` / `upload_empty`);
  - applies the xlsx zip-bomb guard: ratio > 100 when the file expands beyond 1 MiB, more than 1 GiB uncompressed, or more than 10k parts (`upload_zip_bomb`);
  - computes sha256 and streams the file to MinIO through `apps.core.storage` at `orgs/<org>/uploads/<id>/<name>`;
  - writes an audit event.
- `GET /uploads/{id}` returns the upload.
- Ingest task `apps.datasets.tasks.ingest_dataset` (TenantTask, `long_running=True`, queue `ingest`, short `tenant_step` transactions). Stages and progress events: download 5 → scan 10 → parse 20 → profile 40 → load 60 → quality 80 → finalize 95 → `finish` 100. Each stage is published as both a `status` and a `log` event.
  - **Parsing:**
    - Encoding: BOM, then strict UTF-8, then charset-normalizer (cp1252 preferred on Latin ties), with a UTF-8 fallback; the file is transcoded.
    - Delimiter sniffing is quote-aware for `, ; \t |`. The header is sniffed, and options can override both.
    - Ragged rows give `malformed_file`. Trailing empty columns are dropped.
    - Excel uses python-calamine directly: the first non-empty sheet (or `options.sheet`), cached formula values, and merged two-row headers flattened.
    - JSON accepts an array, a wrapper object with one record list, or NDJSON. It is flattened to `a__b` up to depth 3; deeper objects and arrays become `jsonb`.
    - Caps: 1,000 columns and 50M rows (`DATASET_MAX_COLUMNS` and `DATASET_MAX_ROWS` settings are optional).
  - **Column names:** snake_case ASCII, unique, ≤ 63 characters. A leading digit gets a `c_` prefix, and PG system column names get a `_` suffix. `source_name` keeps the original.
  - **Types:** the ladder is boolean → bigint → numeric/double precision → date → timestamptz → text, with the ≥ 99.5% rule.
    - Null tokens are ignored for typed columns.
    - Leading-zero codes and numbers with more than 15 digits stay text.
    - Currency handles ₹/$/€/£/Rs/INR, Indian and western grouping, and accounting negatives; percent values are handled too.
    - Dates prefer day-first, then month-first; timestamps are normalized to UTC.
    - Failed casts are counted as `invalid_count` (validity).
  - **Semantic types:** id, email, phone, url, currency, percent, date, category, free_text, person_name, geo_country, geo_city, boolean, number, json, text.
  - **PII:** column-name heuristics plus a regex hit rate ≥ 50% over email, phone, PAN, Aadhaar, SSN, Luhn card numbers and IPv4. This sets `is_pii`, `pii_type` and `pii_source`.
  - **Profile:** null %, distinct (approximate above 1M rows), min/max/mean/stddev/p1/p5/p50/p95/p99, text length stats, top-10 values (suppressed for PII), and a 20-bin histogram (numeric and temporal).
  - **Dataset level:** duplicate rows, candidate keys, correlated pairs, `invalid_cells`, and schema drift against the previous version.
  - **After profiling:** warehouse load, quality, the lineage edge `upload → dataset_version`, and an audit event.
  - **Atomic switch:** `current_version` changes only if no blocking rule failed.
- Asset classifier: `register_asset_classifier("dataset", services.dataset_classification)`.
- Lineage node resolvers are registered for `dataset`, `dataset_version` and `upload`.
- APIs (permission codenames from the catalog):

  | Endpoint | Behavior | Permission |
  |---|---|---|
  | `GET /datasets` | `?search=` (name/description/slug/tag), plus `?status=`, `?classification=`, `?tag=`; cursor-paginated | `datasets.view` |
  | `POST /datasets` | 202 `{dataset, job_id, status_url, events_url}`; accepts optional `options {sheet, delimiter, has_header, encoding}` | `datasets.create`; a non-internal classification also needs `datasets.classify` + `attest=true` |
  | `GET /datasets/{id}` | contract `Dataset` shape, plus `source_kind`, `classification_attested_at`, `owner_id`, `last_error`, `last_job_id` | `datasets.view` |
  | `PATCH /datasets/{id}` | name, description, tags; classification is rejected here | `datasets.manage` |
  | `DELETE /datasets/{id}` | soft delete | `datasets.manage` |
  | `POST /datasets/{id}/classification` | `{classification, attest}`; `attest=true` is required for synthetic/public (422 `attestation_required`); audited with before/after | `datasets.classify` |
  | `GET /datasets/{id}/columns[?version=n]` | column metadata and stats | `datasets.view` |
  | `GET /datasets/{id}/preview?limit=1..1000[&version=n]` | goes through the read-only executor; 409 `dataset_not_ready` | `datasets.view` |
  | `GET /datasets/{id}/versions` | version list (extension) | `datasets.view` |
  | `POST /datasets/{id}/versions {upload_id}` | 202, new version (extension) | `datasets.create` |
  | `GET /data-stats` | stats (see Open issues for pipeline fields) | `datasets.view` |

### C. Quality (`backend/apps/quality/`)
- Models: `QualityRule`, `QualityResult` and `QualityReport`, all with RLS. The report is unique per `(org, dataset_version)`.
- `engine.py` (pure Polars):
  - Validates and evaluates `not_null`, `unique` (single or composite `params.columns`), `range` (numeric or date, inclusive or exclusive), `regex` (full match, Rust regex so there is no catastrophic backtracking), `allowed_values`, `type`, `freshness`, `row_count`, `referential` (another dataset's current version) and `expression`.
  - `expression` is a safe SQL subset parsed by sqlglot and compiled to Polars. It allows only columns, literals, comparisons, AND/OR/NOT, arithmetic, IN, BETWEEN, IS NULL, LIKE/ILIKE, LOWER/UPPER/LENGTH/TRIM/ABS/COALESCE. NULL counts as a pass.
  - Sample failures: at most 20 rows, PII values masked.
- Rule suggestions from the profile: `not_null` (0% nulls), `unique` (candidate keys that are id-like or email), `range` (p1/p99 ± 10%, clamped at 0 for non-negative data) and `allowed_values` (≤ 10 distinct, non-PII).
- `duplicates.py`:
  - exact duplicate rows;
  - key duplicates per key column (samples masked for PII);
  - fuzzy clusters on non-PII text, category and geo columns: distinct values, normalization, 2-character prefix blocking (re-blocked at 3 characters if a block exceeds 1,500), rapidfuzz `token_sort_ratio ≥ 90` or a Levenshtein typo rule (1 edit for 5–8 characters, 2 edits for 9+), then union-find.
- Score (0–100) = 50% severity-weighted rule pass rate (error ×3, warn ×1, info ×0) + 50% mean of the four dimensions. If no rules carry weight, the score is the dimension mean. The dimensions are:
  - completeness: share of non-null cells;
  - uniqueness: exact rows, key columns and unique rules;
  - validity: invalid cells from type inference plus validity rules;
  - consistency: rows in non-representative fuzzy-cluster variants.
  - `freshness` is added when freshness rules exist.
- APIs:
  - `GET /datasets/{id}/quality[?version=n]` returns the contract shape `{score, dimensions, rules:[{id,column,rule_type,severity,passed,failed_count,...}], duplicates:{exact_rows,key_duplicates,fuzzy_clusters,key_columns}}` plus `status`, `summary` and `generated_at`.
  - `GET/POST /datasets/{id}/quality-rules` (POST needs `quality.manage`) returns each rule with its `last_result`.
  - `GET/PATCH/DELETE /datasets/{id}/quality-rules/{rule_id}`.
  - `POST /datasets/{id}/quality/evaluate` returns 202 and runs the job `apps.quality.tasks.evaluate_dataset_quality`.
  - Rule changes are audited.

### D. Lineage (`backend/apps/lineage/`)
- `LineageEdge` has RLS and a unique key on `(org, from, to, run_id)` with `nulls_distinct=False`.
- `services.record(...)` is idempotent.
- `services.graph(node_type, node_id, depth≤6)` runs a BFS upstream and downstream. Apps register node resolvers through `register_node_resolver` (label, visibility, structural edges such as `dataset_version → dataset` "version of"). Nodes that cannot be resolved are collapsed into a `restricted` placeholder.
- `GET /lineage?node_type=&node_id=&depth=` returns `{nodes:[{id,type,label}], edges:[{from,to,label}]}`. It gives 400 on bad params and 404 when the root is unknown or belongs to another org.

### E. Tests (all synthetic data)

**DB-free: executed, 203 passed / 0 failed.**

Command: `$env:PYTEST_DB_SUFFIX="data_engineering"; python -m pytest apps/datasets/tests/test_filetypes.py apps/datasets/tests/test_parsing.py apps/datasets/tests/test_inference.py apps/datasets/tests/test_profiling.py apps/quality/tests/test_engine.py apps/warehouse/tests/test_naming.py -p no:cacheprovider`

| File | Covers |
|---|---|
| `test_filetypes.py` | allow-list, magic bytes, UTF-16, zip bomb |
| `test_parsing.py` | UTF-8/BOM/UTF-16/cp1252, the 4 delimiters, quoted delimiters and newlines, header sniffing, ragged and empty files, column cap, Excel (sheet choice, typed cells, merged header, empty and corrupt workbooks), JSON nesting/NDJSON/wrapper/scalars/malformed |
| `test_inference.py` | type ladder (18 cases), 99.5% rule, null tokens, date order, UTC, currency/Indian grouping, column names |
| `test_profiling.py` | semantic types, PII by name and by value, stats, histogram, dataset profile, drift, `analyze()` end to end |
| `quality/test_engine.py` | every rule type, 17 validation cases, expression allow/deny, suggestions, fuzzy clusters, key/exact duplicates, dimensions and score |
| `warehouse/test_naming.py` | warehouse naming and serialization |

**DB-backed: 47 tests written, not executed (infra unavailable).** Postgres on `localhost:5433` timed out, Redis on `localhost:6380` refused the connection, and MinIO on `localhost:9000` was unreachable during the whole session. Pytest errors at test-DB setup with `OperationalError: connection timeout expired`.

| File | Tests | Covers |
|---|---|---|
| `apps/warehouse/tests/test_warehouse.py` | 16 | idempotent provisioning and grants, binding RLS, the `org_created` signal, typed load and read-back, immutability, `''` vs NULL, executor caps, params, `current_user`, other-org refusal, **reader role cannot read another org's schema (P3-AC6)**, writes and session changes refused, statement timeout, preview |
| `apps/datasets/tests/test_api.py` | 21 | upload happy path and rejections (type, mismatch, empty, zip bomb, 413, permissions); **CSV end to end (P3-AC1)**: job, columns, PII, preview, quality, lineage, data-stats, audit, classifier; XLSX and JSON ingest; malformed file → failed job code; new version with drift; validation; list, search, PATCH, soft delete; classification attestation and permission; viewer read-only; preview limits and 409; **cross-org 404 on every endpoint** |
| `apps/quality/tests/test_api.py` | 5 | suggested rules, rule CRUD and validation, expression rule plus the re-evaluate job, a blocking rule keeping the previous version current, permissions and isolation |
| `apps/lineage/tests/test_lineage.py` | 5 | idempotent `record`, graph expansion, restricted placeholder, tenant isolation, API 400/404 |

Command, once Docker is up: `$env:PYTEST_DB_SUFFIX="data_engineering"; python -m pytest apps/warehouse apps/datasets apps/quality apps/lineage --create-db -p no:cacheprovider`

The DB tests use an in-memory object-storage fake (it still enforces the `orgs/<org_id>/` prefix), the real `nexora_app` (RLS) and the real `nexora_wh`. Warehouse objects with the session prefix `t_<suffix>_` are dropped at session end. Objects with the `org_` prefix are never touched.

`ruff check` is clean on all four apps. `scripts/check_conventions.py` gives "conventions OK". `scripts/check_ownership.py --agent data_engineering` gives "ownership OK". `manage.py check` is clean, and `makemigrations --check` reports no changes.

## Requests (Integrator)
- **[contract]** Additive response fields and endpoints. Please document them in CONTRACT.md:
  - `Dataset` gains `source_kind`, `classification_attested_at`, `owner_id`, `last_error {code,message}`, `last_job_id`.
  - `Dataset.status` values: `processing | ready | failed | archived | deleted`. The pseudo-DDL has `active`; the UI needs processing and failed.
  - The 202 body also carries `status_url`.
  - New endpoints: `GET /uploads/{id}`, `GET/POST /datasets/{id}/versions`, `GET/PATCH/DELETE /datasets/{id}/quality-rules/{rule_id}`, `POST /datasets/{id}/quality/evaluate`.
  - The quality response adds `status`, `summary`, `generated_at` and `duplicates.key_columns`. `duplicates.key_duplicates` is an integer: the total extra rows sharing a key value.
  - `GET /datasets/{id}/columns` returns a bare array, with extra stats keys.
- **[contract]** Lineage graph node `id`s are raw UUID strings, plus a synthetic node `{"id": "restricted", "type": "restricted"}` when assets are hidden. The endpoint returns 404 when the root node is not visible.
- **[dep, optional]** `fastexcel` would allow `pl.read_excel(engine="calamine")`. Not needed: python-calamine is used directly. The `.xls` path is covered only by the magic-byte test, because no xls writer (xlwt) is installed to generate fixtures.
- **[setting, optional]** `DATASET_MAX_COLUMNS` (default 1000) and `DATASET_MAX_ROWS` (default 50,000,000) are read with `getattr`. Add them to `.env.example` and settings if they should be configurable per deployment.
- **[infra]** `WH_DATABASE_URL_EXECUTOR` must keep `wh_executor` NOINHERIT. `wh_loader` must keep CREATEROLE, because on PG ≥ 16 the creator holds ADMIN on the reader roles and runs `GRANT wh_q_* TO wh_executor`. Both are already set in `infra/postgres/init/00-init.sh`.
- **[backend/core]** `TenantTask.__call__` sets `current_actor` for eager runs outside a request without resetting it. It is harmless, but it leaks into the calling thread in tests.

## Open issues / known limitations
- **All DB-backed tests (47) have not been executed.** They were written against the real schema and roles and reviewed by hand, but they need a run once Postgres, Redis and MinIO are up. Expect possible small fixes.
- `data-stats.pipelines_active` and `runs_7d` are 0 until wave-2 pipelines register a provider through `apps.datasets.services.register_stats_provider(fn)`.
- Preview applies no column deny/mask yet (out of scope for W1). In wave 2 the SQL guard and policies wrap `execute_readonly_sql`. The executor is low-level and must only be called with guard-validated SQL. Its denylist is defense in depth, not the control.
- Profile-suggested rules are created active, with warn/info/error severities but never blocking. Admins can deactivate them, delete them or set them to blocking.
- `byte_cap` is an estimate based on value text length.
- `read_table` returns `numeric` as Float64.
- Fuzzy clustering misses typos in the first 2 characters (a blocking trade-off), and at most 20,000 distinct values per column are compared.
- Retiring old warehouse versions (retention) and the org purge job are not implemented. `drop_table` and `drop_org_objects` are ready for them.
- Hindi and Gujarati PII detection is not covered, which is the documented gap. Presidio is not used: there are no new dependencies.
- Wave-2 hooks ready to use:
  - `datasets.services.load_version_frame` / `load_current_frame` / `parse_as_type`;
  - `quality.services.compute` / `persist` / `load_rule_specs` (for `quality.gate`);
  - `lineage.services.record` and `register_node_resolver`;
  - `warehouse.services.load_table` / `physical_table` / `reader_role` / `execute_readonly_sql` (for sqlguard).
