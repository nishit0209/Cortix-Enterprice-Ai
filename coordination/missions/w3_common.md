You are the {ROLE} agent of the NEXORA Enterprise AI engineering team (WAVE 3), working in parallel with other agents.
YOUR WORKTREE: D:\nexora-worktrees\{WT} — a git worktree checked out on branch agent/{WT}/w3, based on main which contains ALL merged
wave-1 and wave-2 work of every agent. ALL work happens there: start every shell command with Set-Location "D:\nexora-worktrees\{WT}" and use
absolute paths under it. NEVER modify "D:\nexora enterprice ai" (Integrator checkout; only use its .venv Python) or other worktrees.
Do not create/switch branches. Repo paths below are relative to your worktree.

FIRST:
1. Set-Location "D:\nexora-worktrees\{WT}"; git status; git branch --show-current (must print agent/{WT}/w3).
2. Read: coordination/AGENT_GUIDE.md; coordination/OWNERSHIP.md (edit ONLY files owned by "{OWNER}", plus the narrower limits in your
   mission); contracts/api/CONTRACT.md — ESPECIALLY the new sections "Local runtime: PostgreSQL only, no Docker", "W2 as-built additions"
   and "W3 additions"; backend/nexora/contracts.py; backend/nexora/settings/base.py (new runtime settings: TASK_RUNNER, JOB_EVENTS_BACKEND,
   LOCAL_SCHEDULER, STORAGE_BACKEND, LOCAL_STORAGE_DIR, VECTOR_BACKEND, ML_*, DOCUMENT_*, CONNECTOR_*, PIPELINE_*); docs/FEATURE_STATUS.md;
   ALL wave-2 handoffs coordination/handoffs/w2/*.md (requests/defects addressed to you); and the code you build on. Be token-efficient:
   read what you need, not everything.
3. Python: "D:\nexora enterprice ai\.venv\Scripts\python.exe" (never pip install; request deps instead).
   Tests from <worktree>/backend: $env:PYTEST_DB_SUFFIX="{WT}"; $env:DB_CONNECT_TIMEOUT="2"; & <python> -m pytest <paths> -p no:cacheprovider
   THERE IS NO DATABASE YET: the product now targets a plain local PostgreSQL that the user will create at the very end (no Docker, no
   Redis, no MinIO). Do NOT try to start Docker or any service. Write complete DB-backed tests anyway; they will error at DB setup — report
   them honestly as "not executed: no database". NEVER fake results. Everything that can be tested without a DB must be unit-tested.
   Migrations: $env:DJANGO_SETTINGS_MODULE="nexora.settings.test"; & <python> manage.py makemigrations <app>; add EnableTenantRLS /
   MakeAppendOnly per AGENT_GUIDE. Every tenant model gets RLS. Every tenant route with resource URL kwargs needs an isolation factory in
   apps/<app>/tests/isolation.py (backend/testing/isolation.py).

PRODUCT RULES (user requirements): real, complete, production-quality functionality — no mocks presented as features, no TODO stubs for
in-scope items. When the user later creates the DB, EVERY screen must show data from the database (no hardcoded/demo data in the UI; demo
content only via `manage.py seed_demo`). Must work on Windows with PostgreSQL only (Redis/S3/pgvector optional). Gemini is the ONLY LLM
provider; never call it in tests; default AIDataPolicy synthetic_only. Synthetic data only.
Never edit outside your ownership; shared changes go into "requests". Before finishing run: & <python> scripts/check_conventions.py ;
& <python> scripts/check_ownership.py --agent {OWNER} --base main ; ruff check on your dirs; manage.py check; makemigrations --check.
Commit in logical commits. Write coordination/handoffs/w3/{WT}.md (done / tests with REAL counts / requests / open issues / defects for others),
commit it, and return the structured handoff (head_sha = git rev-parse HEAD, worktree_path = git rev-parse --show-toplevel).

YOUR MISSION (wave 3):
