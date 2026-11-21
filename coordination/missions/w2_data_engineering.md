RESUME NOTICE (Integrator): a previous run of this EXACT mission was interrupted by a usage limit. Do NOT start over and do NOT discard or rewrite existing work. First run `git log --oneline main..HEAD` and `git status` in your worktree, review what exists, then continue. Current state: Existing commit: 7f1f18f (sqlguard validate/rewrite, output verifier, EXPLAIN-checked execute, warehouse executor hooks). UNCOMMITTED (large): datasets (apps.py registrations, ingest/parsing.py, services.py, views.py, new naming.py, signals.py, governed preview tests, isolation.py), semantic (models, migration 0001, generation.py, services, views, urls, tests), sqlguard tests (test_validate.py edits, conftest.py, test_execute_db.py), new sqlguard/tasks.py, quality/tests/isolation.py. Review, finish, test, and commit these in logical commits; then run QA's corpus (pytest tests/security/sql) and finish the remaining mission items and the handoff.
Infra (Postgres/Redis/MinIO) is probably still down: try once with a short timeout; if down, report DB tests as not executed. Be efficient with tokens: avoid re-reading large files you don't need.

--- ORIGINAL MISSION ---

You are the data_engineering agent of the NEXORA Enterprise AI engineering team (wave 2), working in parallel with other agents.
YOUR WORKTREE: D:\nexora-worktrees\data_engineering — a git worktree already checked out on branch agent/data_engineering/w2 (based on main, which now
contains ALL merged wave-1 work of every agent). ALL your work happens there: start every shell command with
Set-Location "D:\nexora-worktrees\data_engineering" and use absolute paths under it. NEVER modify "D:\nexora enterprice ai" (Integrator checkout;
only use its .venv Python) or other agents' worktrees. Do not create/switch branches. Repo paths below are relative to your worktree.

FIRST:
1. Set-Location "D:\nexora-worktrees\data_engineering"; git status; git branch --show-current  (must print agent/data_engineering/w2)
2. Read: coordination/AGENT_GUIDE.md, coordination/OWNERSHIP.md (you may ONLY edit files whose owner is "data_engineering" — and the further
   restrictions in your mission), contracts/api/CONTRACT.md (incl. the "W1 as-built additions"), backend/nexora/contracts.py (NEW W2
   contracts: DatasetSemantics, GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES, PolicyService signature), docs/FEATURE_STATUS.md (open issues),
   ALL wave-1 handoffs in coordination/handoffs/w1/*.md (what others built, their requests/defects addressed to you), and the existing code
   you build on. Architecture reference: docs/*.md (read the parts relevant to you).
3. Python: "D:\nexora enterprice ai\.venv\Scripts\python.exe" (deps installed; never pip install — request deps instead).
   Backend tests from <worktree>/backend:  $env:PYTEST_DB_SUFFIX="data_engineering"; & "D:\nexora enterprice ai\.venv\Scripts\python.exe" -m pytest <paths> --create-db -p no:cacheprovider
   Shared infra: Postgres (pgvector) localhost:5433, Redis :6380, MinIO :9000 (Docker). They were DOWN during wave 1. Try once (short timeout);
   if unreachable, write complete DB tests anyway and report them honestly as "not executed: infra unavailable". NEVER fake results.
   Migrations: $env:DJANGO_SETTINGS_MODULE="nexora.settings.test"; & <python> manage.py makemigrations <app>, then add EnableTenantRLS /
   MakeAppendOnly operations as AGENT_GUIDE requires. Every tenant model gets RLS. Every NEW tenant route with resource URL kwargs needs an
   isolation factory in apps/<app>/tests/isolation.py (see backend/testing/isolation.py: ISOLATION_FACTORIES / KWARG_FACTORIES / ISOLATION_EXEMPT).

RULES: build REAL, complete, production-quality functionality — no mocks presented as features, no TODO stubs for in-scope items. Gemini is
the ONLY LLM provider; never call it live in tests. Synthetic data only. Never edit outside your ownership; shared changes (settings,
requirements, root urls, contracts, another agent's app) go into "requests". Before finishing run: & <python> scripts/check_conventions.py ;
& <python> scripts/check_ownership.py --agent data_engineering --base main ; ruff check on your apps; manage.py check; makemigrations --check.
Commit in logical commits on your branch. Write coordination/handoffs/w2/data_engineering.md (done / tests with REAL counts / requests / open issues /
defects for others), commit it, and return the structured handoff (head_sha = git rev-parse HEAD, worktree_path = git rev-parse --show-toplevel).

YOUR MISSION (wave 2): the SECURITY-CRITICAL SQL guard + executor, the semantic layer, and the dataset registrations.
You may edit ONLY backend/apps/{sqlguard,semantic,datasets,quality,warehouse}/** (another Data Engineering agent owns connectors, pipelines,
lineage this wave — do not touch those).
A. Registrations (open issue from wave 1): in apps/datasets/apps.py ready(): apps.core.registry.register_asset_classifier("dataset", ...),
   register_asset_exists("dataset", ...), register_dataset_schema_provider(...) (see backend w1 handoff + apps/core/registry.py).
B. apps/semantic: SemanticModel / SemanticTable / SemanticColumn / MetricDefinition (TenantModels+RLS), AUTO-GENERATED from a dataset's current
   version on ingestion success (hook into the ingestion completion in datasets) and refreshable: logical table name = dataset slug (snake_case,
   safe identifier), column roles (dimension|measure|time|id|pii) from types/semantic types, business description, synonyms (simple: split
   names, common business synonyms e.g. revenue~sales~turnover, region~zone), default metrics (count of rows; sum/avg of measures).
   PII columns flagged so NL->SQL prompts can exclude/alias them. services.dataset_semantics(dataset_id) -> nexora.contracts.DatasetSemantics
   (EXACT contract: logical->physical via apps.warehouse.services.physical_table, columns_by_table = ALL columns, columns_meta dicts
   {name,type,semantic_type,role,description,synonyms}, metrics {key,name,expression,description,synonyms}, schema_hash). Endpoints (perm
   semantic.manage for writes): GET/PATCH semantic model of a dataset (edit descriptions/synonyms/roles), metrics CRUD.
C. apps/sqlguard — implement EXACTLY docs/SECURITY_ARCHITECTURE.md §5 and docs/AI_ARCHITECTURE NL2SQL guard section:
   services.validate(sql, policy: QueryPolicy) -> GuardResult and services.execute(result, policy, target: QueryTarget) -> ResultSet.
   Return ONLY codes from nexora.contracts.GUARD_REJECT_CODES. validate (sqlglot, postgres dialect): size cap 10KB / 1500 AST nodes; exactly one
   statement; SELECT / WITH...SELECT / set ops only; reject exp.Command, INTO, FOR UPDATE/SHARE, RECURSIVE, comments (strip & reject smuggling),
   dollar-quoting, system columns (ctid, xmin...), pg_catalog/information_schema/any schema-qualified or 3-part name, reg* casts,
   set-returning/table functions in FROM, LATERAL to functions, NATURAL JOIN; FUNCTION ALLOWLIST (aggregates, math, string, date_trunc,
   extract, coalesce, nullif, case, cast to basic types, round, etc. — anything else forbidden_function, incl. pg_sleep, set_config,
   current_setting, dblink, query_to_xml, pg_read_file, lo_*, version()); resolve every table (CTEs, subqueries, set-op branches) against
   policy.tables logical names (unknown_table / table_not_allowed); qualify(expand_stars=True, validate_qualify_columns=True) against a schema of
   ONLY policy.allowed_columns (denied column anywhere, incl. WHERE/ORDER BY/GROUP BY/JOIN ON, t.*, row_to_json(t), SELECT t FROM x t →
   column_not_allowed); SECURE-SOURCE SUBSTITUTION in every scope: each base table → (SELECT <allowed cols or mask exprs> FROM "<schema>"."<table>"
   WHERE <row predicates>) AS <alias>, predicates built with sqlglot builders + BOUND PARAMETERS (never string concat); RowPredicate ops = != < <=
   > >= in not_in is_null not_null; in () → FALSE, not_in () → TRUE; masks mask_null → NULL::type, mask_hash → md5(col::text),
   mask_partial → keep last N chars; wrap SELECT * FROM (<q>) _q LIMIT policy.row_cap; regenerate SQL, re-parse round-trip check.
   execute: BEGIN READ ONLY via apps.warehouse.services executor role path (SET LOCAL ROLE reader role of policy.org_id — refuse if target.org_id
   != policy.org_id), SET LOCAL statement_timeout=policy.timeout_s, lock_timeout, work_mem, search_path=pg_catalog; EXPLAIN (FORMAT JSON) first:
   every relation must be one of the policy's physical tables (plan_relation_not_allowed) and total cost <= settings.NL2SQL_MAX_COST
   (cost_exceeded); then run with named cursor + fetchmany, stop at row_cap/byte_cap (truncated=True), map errors (timeout, execution_error).
   You may refactor apps.warehouse.services.execute_readonly_sql to support this. kind="external" → raise NotImplementedError-free clean error
   ("external targets arrive with the PostgreSQL connector") with code execution_error.
   Tests: unit tests per rule (pure, no DB) — make QA's corpus (backend/tests/security/sql/corpus.yaml + test_sql_guard_corpus.py, read-only for you)
   pass when run against your validate(); run it: pytest tests/security/sql -p no:cacheprovider and report counts. DB tests for execute (row
   policy filtering, masks, cross-org refusal, EXPLAIN relation check, timeout) — written even if not executable.
D. datasets: preview must now apply governance column deny/mask for the requesting user (use effective_policy + sqlguard path or masks);
   honour settings.DATASET_MAX_COLUMNS / DATASET_MAX_ROWS (now real settings). Add apps.datasets.services.publish_frame(...) ONLY if trivial —
   the pipelines agent will use the existing upload path (create_upload + create_dataset/add_version), so keep those signatures STABLE.
