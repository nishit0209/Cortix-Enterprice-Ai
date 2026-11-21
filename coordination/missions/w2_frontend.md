RESUME NOTICE (Integrator): a previous run of this EXACT mission was interrupted by a usage limit. Do NOT start over and do NOT discard or rewrite existing work. First run `git log --oneline main..HEAD` and `git status` in your worktree, review what exists, then continue. Current state: Existing commits: 3c17a6b (Ask AI workspace), b6b0655 (Data Engineering screens), 79a52eb (Documents, dashboard wiring, tests). UNCOMMITTED: a small edit to src/components/ask/ask-workspace.tsx. Review it, finish anything missing from the mission, run lint/typecheck/tests/build, commit, and write the handoff.
Infra (Postgres/Redis/MinIO) is probably still down: try once with a short timeout; if down, report DB tests as not executed. Be efficient with tokens: avoid re-reading large files you don't need.

--- ORIGINAL MISSION ---

You are the frontend agent of the NEXORA Enterprise AI engineering team (wave 2), working in parallel with other agents.
YOUR WORKTREE: D:\nexora-worktrees\frontend — a git worktree already checked out on branch agent/frontend/w2 (based on main, which now
contains ALL merged wave-1 work of every agent). ALL your work happens there: start every shell command with
Set-Location "D:\nexora-worktrees\frontend" and use absolute paths under it. NEVER modify "D:\nexora enterprice ai" (Integrator checkout;
only use its .venv Python) or other agents' worktrees. Do not create/switch branches. Repo paths below are relative to your worktree.

FIRST:
1. Set-Location "D:\nexora-worktrees\frontend"; git status; git branch --show-current  (must print agent/frontend/w2)
2. Read: coordination/AGENT_GUIDE.md, coordination/OWNERSHIP.md (you may ONLY edit files whose owner is "frontend" — and the further
   restrictions in your mission), contracts/api/CONTRACT.md (incl. the "W1 as-built additions"), backend/nexora/contracts.py (NEW W2
   contracts: DatasetSemantics, GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES, PolicyService signature), docs/FEATURE_STATUS.md (open issues),
   ALL wave-1 handoffs in coordination/handoffs/w1/*.md (what others built, their requests/defects addressed to you), and the existing code
   you build on. Architecture reference: docs/*.md (read the parts relevant to you).
3. Python: "D:\nexora enterprice ai\.venv\Scripts\python.exe" (deps installed; never pip install — request deps instead).
   Backend tests from <worktree>/backend:  $env:PYTEST_DB_SUFFIX="frontend"; & "D:\nexora enterprice ai\.venv\Scripts\python.exe" -m pytest <paths> --create-db -p no:cacheprovider
   Shared infra: Postgres (pgvector) localhost:5433, Redis :6380, MinIO :9000 (Docker). They were DOWN during wave 1. Try once (short timeout);
   if unreachable, write complete DB tests anyway and report them honestly as "not executed: infra unavailable". NEVER fake results.
   Migrations: $env:DJANGO_SETTINGS_MODULE="nexora.settings.test"; & <python> manage.py makemigrations <app>, then add EnableTenantRLS /
   MakeAppendOnly operations as AGENT_GUIDE requires. Every tenant model gets RLS. Every NEW tenant route with resource URL kwargs needs an
   isolation factory in apps/<app>/tests/isolation.py (see backend/testing/isolation.py: ISOLATION_FACTORIES / KWARG_FACTORIES / ISOLATION_EXEMPT).

RULES: build REAL, complete, production-quality functionality — no mocks presented as features, no TODO stubs for in-scope items. Gemini is
the ONLY LLM provider; never call it live in tests. Synthetic data only. Never edit outside your ownership; shared changes (settings,
requirements, root urls, contracts, another agent's app) go into "requests". Before finishing run: & <python> scripts/check_conventions.py ;
& <python> scripts/check_ownership.py --agent frontend --base main ; ruff check on your apps; manage.py check; makemigrations --check.
Commit in logical commits on your branch. Write coordination/handoffs/w2/frontend.md (done / tests with REAL counts / requests / open issues /
defects for others), commit it, and return the structured handoff (head_sha = git rev-parse HEAD, worktree_path = git rev-parse --show-toplevel).

YOUR MISSION (wave 2): the flagship Conversational BI workspace + the Data Engineering screens. Owned: frontend/** only.
Read contracts/api/CONTRACT.md fully (W2 + W3 + "W1 as-built additions") — the backend implements W3 Conversational BI THIS wave, Data
Engineering implements connectors/pipelines/semantic, AI/ML implements documents. Endpoints not yet on main: build against the contract and
keep graceful 'not available yet' states for 404/501. Keep the wave-1 design language (premium, original, light/dark, motion).
A. /o/[org]/ask — Ask AI workspace (THE flagship screen): left rail conversations (new / rename / delete, grouped by date) with dataset picker
   (only ready datasets); main chat: user bubbles, assistant cards that render the live SSE stream (fetch-based POST stream with
   Accept: text/event-stream; events status/sql/result/insight/token/done/error) — animated stage stepper (authorizing → resolving →
   generating → validating → executing → explaining), SQL transparency panel (executed SQL with syntax highlighting, source badge cache/builder/
   llm/manual, copy, "open in SQL editor"), result DataTable, auto chart (Recharts: bar/line/area/pie/kpi per chart hint, chart type switcher,
   themed), insight bullets, assumptions, follow-up suggestions chips, thumbs up/down feedback, export CSV/XLSX buttons, error cards per code
   (ai_policy_blocked → explain policy + link to settings/ai-policy; ai_quota_exhausted → resets_at countdown; ai_not_configured; guard reject
   codes → friendly text + show SQL). Composer with keyboard shortcuts, example questions derived from dataset columns. AI status pill/budget.
   Also /o/[org]/ask/sql manual guarded SQL editor (perm bi.sql) and /o/[org]/ask/history query history table (verdict, source, duration).
B. Data Engineering screens: /data/sources (list, "Add PostgreSQL source" dialog with test-connection flow, introspect browser schemas→tables→
   columns, import-table dialog with job progress); /data/pipelines (list + status) and /data/pipelines/[id] visual builder with @xyflow/react:
   node palette (sources, transforms, destination per the node types in the data_pipelines brief: select_columns, rename, cast, filter,
   fill_null, drop_nulls, deduplicate, trim_whitespace, change_case, derive, sort, aggregate, join), per-node config side panel with forms,
   validation errors on nodes, save version, preview (first rows), run now (job progress), schedule editor (cron presets + custom);
   /data/pipelines/[id]/runs + run detail (steps timeline, rows in/out, logs, retry). /data/lineage graph (React Flow, auto layout, node types
   colored, restricted nodes). /data/quality overview (scores per dataset, failing rules). Dataset detail: add "Semantic" tab (edit
   descriptions/synonyms/roles, metrics) and "Lineage" tab.
C. /o/[org]/documents (AI/ML builds the API this wave): upload (drag-drop, job progress), list with status/classification/pages, detail with
   page viewer (text per page), chunks, entities, tables, search box (hybrid search results with highlights), Ask-your-documents panel showing
   answers with citations (click → page) and the "extractive (policy)" badge when the LLM is not allowed.
D. Dashboard: wire the insights panel to GET /insights and recent queries to /bi/query-history; KPI deltas when data exists.
E. Tests (vitest): SSE chat stream reducer (event ordering, error, reconnect rules), chart-hint → chart mapping, pipeline DAG
   validation/serialization helpers, a couple of component tests. Run npm run lint, npm run typecheck, npm test -- --run, npm run build — report
   real results. Write API types from the contract (no generated client yet).
