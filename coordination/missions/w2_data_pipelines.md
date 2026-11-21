RESUME NOTICE (Integrator): a previous run of this EXACT mission was interrupted by a usage limit. Do NOT start over and do NOT discard or rewrite existing work. First run `git log --oneline main..HEAD` and `git status` in your worktree, review what exists, then continue. Current state: Existing commits: f11a4f6 (lineage node types), 54c54a6 (PostgreSQL connector), 87bbf51 (pipelines engine + API). UNCOMMITTED NEW test files: connectors/tests/{conftest,fakes,isolation,test_api,test_live_postgres}.py and pipelines/tests/{isolation,test_api}.py. Review, make the non-DB parts pass, commit, then finish remaining mission items (stats provider, scheduler registration, lineage edges per run, etc. if missing) and the handoff.
Infra (Postgres/Redis/MinIO) is probably still down: try once with a short timeout; if down, report DB tests as not executed. Be efficient with tokens: avoid re-reading large files you don't need.

--- ORIGINAL MISSION ---

You are the data_pipelines agent of the NEXORA Enterprise AI engineering team (wave 2), working in parallel with other agents.
YOUR WORKTREE: D:\nexora-worktrees\data_pipelines — a git worktree already checked out on branch agent/data_pipelines/w2 (based on main, which now
contains ALL merged wave-1 work of every agent). ALL your work happens there: start every shell command with
Set-Location "D:\nexora-worktrees\data_pipelines" and use absolute paths under it. NEVER modify "D:\nexora enterprice ai" (Integrator checkout;
only use its .venv Python) or other agents' worktrees. Do not create/switch branches. Repo paths below are relative to your worktree.

FIRST:
1. Set-Location "D:\nexora-worktrees\data_pipelines"; git status; git branch --show-current  (must print agent/data_pipelines/w2)
2. Read: coordination/AGENT_GUIDE.md, coordination/OWNERSHIP.md (you may ONLY edit files whose owner is "data_engineering" — and the further
   restrictions in your mission), contracts/api/CONTRACT.md (incl. the "W1 as-built additions"), backend/nexora/contracts.py (NEW W2
   contracts: DatasetSemantics, GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES, PolicyService signature), docs/FEATURE_STATUS.md (open issues),
   ALL wave-1 handoffs in coordination/handoffs/w1/*.md (what others built, their requests/defects addressed to you), and the existing code
   you build on. Architecture reference: docs/*.md (read the parts relevant to you).
3. Python: "D:\nexora enterprice ai\.venv\Scripts\python.exe" (deps installed; never pip install — request deps instead).
   Backend tests from <worktree>/backend:  $env:PYTEST_DB_SUFFIX="data_pipelines"; & "D:\nexora enterprice ai\.venv\Scripts\python.exe" -m pytest <paths> --create-db -p no:cacheprovider
   Shared infra: Postgres (pgvector) localhost:5433, Redis :6380, MinIO :9000 (Docker). They were DOWN during wave 1. Try once (short timeout);
   if unreachable, write complete DB tests anyway and report them honestly as "not executed: infra unavailable". NEVER fake results.
   Migrations: $env:DJANGO_SETTINGS_MODULE="nexora.settings.test"; & <python> manage.py makemigrations <app>, then add EnableTenantRLS /
   MakeAppendOnly operations as AGENT_GUIDE requires. Every tenant model gets RLS. Every NEW tenant route with resource URL kwargs needs an
   isolation factory in apps/<app>/tests/isolation.py (see backend/testing/isolation.py: ISOLATION_FACTORIES / KWARG_FACTORIES / ISOLATION_EXEMPT).

RULES: build REAL, complete, production-quality functionality — no mocks presented as features, no TODO stubs for in-scope items. Gemini is
the ONLY LLM provider; never call it live in tests. Synthetic data only. Never edit outside your ownership; shared changes (settings,
requirements, root urls, contracts, another agent's app) go into "requests". Before finishing run: & <python> scripts/check_conventions.py ;
& <python> scripts/check_ownership.py --agent data_engineering --base main ; ruff check on your apps; manage.py check; makemigrations --check.
Commit in logical commits on your branch. Write coordination/handoffs/w2/data_pipelines.md (done / tests with REAL counts / requests / open issues /
defects for others), commit it, and return the structured handoff (head_sha = git rev-parse HEAD, worktree_path = git rev-parse --show-toplevel).

YOUR MISSION (wave 2): external PostgreSQL connector + the pipeline engine (builder model, runner, scheduler, retries, run history) + pipeline
lineage. You are a SECOND Data Engineering agent: you may edit ONLY backend/apps/{connectors,pipelines,lineage}/** (the other DE agent owns
datasets, quality, warehouse, semantic, sqlguard this wave — call their services, never edit them; keep to the public functions that exist on main).
Read docs/DATA_ENGINEERING_DESIGN.md and SECURITY_ARCHITECTURE (connectors/SSRF/secrets) first.
A. apps/connectors: DataSource (TenantModel+RLS: name, type "postgresql", config JSON {host,port,dbname,sslmode,schema}, credential ref,
   status, last_tested_at, last_error), Credential (envelope-encrypted via apps.core.crypto.encrypt_secret; secret NEVER returned/logged).
   Connector plugin interface (BaseConnector: test(), introspect(), read_batches(table, columns, batch_rows), incremental_cursor) + PostgresConnector
   (psycopg): SSRF guard — resolve host once, reject private/loopback/link-local/multicast/reserved unless settings.ALLOW_PRIVATE_DB_HOSTS, connect
   with hostaddr pinning; connect_timeout; default sslmode require (verify-full option); on save/test verify the role is read-only: not superuser,
   no BYPASSRLS, no CREATE on db/schema, no write privileges on tables (has_table_privilege) — refuse otherwise with a clear code
   (connector_role_not_readonly). Endpoints per CONTRACT W2: GET/POST data-sources (password write-only), GET/PATCH/DELETE, POST /test,
   POST /introspect (schemas → tables → columns with types + estimated rows), POST /import {table, name, columns?, classification?} → 202 job
   (Celery TenantTask, queue "ingest"): stream rows in batches to a CSV in object storage, then apps.datasets.services.create_upload(...) +
   create_dataset(...) (or add_version for re-imports) so ingestion/profiling/quality/warehouse/semantic all reuse the standard path;
   record lineage source→dataset. Perms sources.view / sources.manage. Audit create/update/delete/test/import.
B. apps/pipelines: Pipeline, PipelineVersion (DAG JSON validated with pydantic: nodes [{id, type, config}], edges), PipelineRun (status queued|
   running|succeeded|failed|cancelled, attempt, started_at, finished_at, duration_ms, rows_in, rows_out, rows_rejected, error_code,
   error_message, quality_summary, steps JSON), PipelineRunLog, PipelineSchedule (cron, timezone, enabled, next_run_at). Node types (linear DAG
   source→steps→destination, validated: exactly one source, one destination, acyclic, connected): source.dataset {dataset_id}, source.connector
   {data_source_id, table}; transform steps executed with Polars: select_columns, rename, cast, filter (structured conditions — NO eval/SQL
   strings), fill_null, drop_nulls, deduplicate (subset), trim_whitespace, change_case, derive (safe expression builder from a whitelisted op set),
   sort, aggregate (group_by + aggs), join (with another dataset, key mapping); destination.dataset {name | dataset_id (new version)}.
   Runner = TenantTask on queue "ingest": loads input frames (apps.datasets.services.load_current_frame), applies steps recording per-step rows
   in/out + duration, writes the result via the upload path (CSV → create_upload → create_dataset/add_version) so quality+profile run; job progress
   events; retries with exponential backoff up to pipeline.max_retries on retryable errors; failure → run failed + error + audit + notification
   hook. Scheduler: a Celery beat dispatcher task (runs as the ops alias — the documented BYPASSRLS dispatcher pattern) that enqueues due
   schedules per org with croniter; register via django_celery_beat PeriodicTask in a data migration or in AppConfig.ready (document the choice).
   Endpoints per CONTRACT W2: pipelines CRUD (+ dag validation errors per node), POST /pipelines/{id}/runs (202 job), runs list, run detail
   (steps + logs), POST /pipeline-runs/{id}/retry, PUT /pipelines/{id}/schedule, POST /pipelines/{id}/preview (run the DAG on the first 200 rows
   synchronously, no writes). Register a datasets stats provider (apps.datasets.services.register_stats_provider) so data-stats reports
   pipelines_active and runs_7d. Perms pipelines.view / pipelines.manage / pipelines.run.
C. apps/lineage: add node types data_source, pipeline, pipeline_run; record source→pipeline→dataset edges per run; keep GET /lineage contract.
D. Tests: DAG validation, every transform (pure Polars), runner success/failure/retry (fakes for storage/ingestion where needed), SSRF guard
   (private IPs, DNS rebinding pinning), read-only role verification logic, credential never in API output/logs, API + cross-org 404 tests.
