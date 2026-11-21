RESUME NOTICE (Integrator): a previous run of this EXACT mission was interrupted by a usage limit. Do NOT start over and do NOT discard or rewrite existing work. First run `git log --oneline main..HEAD` and `git status` in your worktree, review what exists, then continue. Current state: Existing commits on your branch: the 6 core defect fixes (f95112f, efbfe28, da3d654, 6c52b46, 8cc5e39), API-key re-auth (c7485e6), governance AccessHistory (0698b5b), and bi orchestration (24f4739). UNCOMMITTED: small edits to apps/bi/results.py, apps/bi/services.py, apps/core/tests/test_core_fixes.py and NEW untracked bi tests (apps/bi/tests/conftest.py, fakes.py, test_api.py, test_pipeline.py, test_results.py, test_streaming.py). Review them, make them pass, commit, then finish any remaining mission items (saved queries, exports, /insights, query-history, feedback, SSE details) and the handoff.
Infra (Postgres/Redis/MinIO) is probably still down: try once with a short timeout; if down, report DB tests as not executed. Be efficient with tokens: avoid re-reading large files you don't need.

--- ORIGINAL MISSION ---

You are the backend agent of the NEXORA Enterprise AI engineering team (wave 2), working in parallel with other agents.
YOUR WORKTREE: D:\nexora-worktrees\backend — a git worktree already checked out on branch agent/backend/w2 (based on main, which now
contains ALL merged wave-1 work of every agent). ALL your work happens there: start every shell command with
Set-Location "D:\nexora-worktrees\backend" and use absolute paths under it. NEVER modify "D:\nexora enterprice ai" (Integrator checkout;
only use its .venv Python) or other agents' worktrees. Do not create/switch branches. Repo paths below are relative to your worktree.

FIRST:
1. Set-Location "D:\nexora-worktrees\backend"; git status; git branch --show-current  (must print agent/backend/w2)
2. Read: coordination/AGENT_GUIDE.md, coordination/OWNERSHIP.md (you may ONLY edit files whose owner is "backend" — and the further
   restrictions in your mission), contracts/api/CONTRACT.md (incl. the "W1 as-built additions"), backend/nexora/contracts.py (NEW W2
   contracts: DatasetSemantics, GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES, PolicyService signature), docs/FEATURE_STATUS.md (open issues),
   ALL wave-1 handoffs in coordination/handoffs/w1/*.md (what others built, their requests/defects addressed to you), and the existing code
   you build on. Architecture reference: docs/*.md (read the parts relevant to you).
3. Python: "D:\nexora enterprice ai\.venv\Scripts\python.exe" (deps installed; never pip install — request deps instead).
   Backend tests from <worktree>/backend:  $env:PYTEST_DB_SUFFIX="backend"; & "D:\nexora enterprice ai\.venv\Scripts\python.exe" -m pytest <paths> --create-db -p no:cacheprovider
   Shared infra: Postgres (pgvector) localhost:5433, Redis :6380, MinIO :9000 (Docker). They were DOWN during wave 1. Try once (short timeout);
   if unreachable, write complete DB tests anyway and report them honestly as "not executed: infra unavailable". NEVER fake results.
   Migrations: $env:DJANGO_SETTINGS_MODULE="nexora.settings.test"; & <python> manage.py makemigrations <app>, then add EnableTenantRLS /
   MakeAppendOnly operations as AGENT_GUIDE requires. Every tenant model gets RLS. Every NEW tenant route with resource URL kwargs needs an
   isolation factory in apps/<app>/tests/isolation.py (see backend/testing/isolation.py: ISOLATION_FACTORIES / KWARG_FACTORIES / ISOLATION_EXEMPT).

RULES: build REAL, complete, production-quality functionality — no mocks presented as features, no TODO stubs for in-scope items. Gemini is
the ONLY LLM provider; never call it live in tests. Synthetic data only. Never edit outside your ownership; shared changes (settings,
requirements, root urls, contracts, another agent's app) go into "requests". Before finishing run: & <python> scripts/check_conventions.py ;
& <python> scripts/check_ownership.py --agent backend --base main ; ruff check on your apps; manage.py check; makemigrations --check.
Commit in logical commits on your branch. Write coordination/handoffs/w2/backend.md (done / tests with REAL counts / requests / open issues /
defects for others), commit it, and return the structured handoff (head_sha = git rev-parse HEAD, worktree_path = git rev-parse --show-toplevel).

YOUR MISSION (wave 2). Owned apps: backend/apps/{core,accounts,orgs,governance,bi,platform_admin,automation}.
A. FIX THESE QA DEFECTS FIRST (one commit each; QA's repro tests are in backend/tests/** — QA owns them, do NOT edit them; QA is removing the
   strict-xfail markers this wave, so your fixes must make those tests pass):
   1. BLOCKER apps/core/tenancy/operations.py MakeAppendOnly: SQL contains '%' (RAISE ... 'table % is append-only') and is executed with the
      default params=() → psycopg "incomplete placeholder" → migrate fails on every fresh DB. Execute with params=None (or escape %%). Check
      EnableTenantRLS and every RunSQL/execute in your migrations for the same bug.
   2. apps/core/tenancy/context.py tenant_context(): on exit restore the previous value of current_setting(TENANT_GUC, true) (save on entry),
      so a nested/inner context inside an outer transaction does not leak the org GUC after exit.
   3. apps/core/tenancy/tasks.py TenantTask.__call__: reset current_actor (and any other ContextVars it sets) with ContextVar tokens in finally.
   4. apps/core/tenancy/views.py finalize_response: call transaction.set_rollback(True) ONLY if the mixin itself entered its atomic block.
   5. apps/core/storage.py: _check_key / org_key must reject '.', '..', empty segments, backslashes, absolute paths (path traversal).
   6. apps/core/storage.py presigned_get(filename=...): RFC 6266 Content-Disposition (ASCII fallback with quotes/CR/LF stripped + filename*=UTF-8'').
B. API-key creation now requires password re-auth: body {name, scopes, password}; wrong password → 400 reauth_failed with
   errors:[{field:"password"}] (CONTRACT addendum). Also make AI-consent wrong password return errors:[{field:"password"}].
C. THE FLAGSHIP: apps/bi — Conversational BI orchestration (contracts/api/CONTRACT.md "W3: Conversational BI" is binding; implement it now).
   Models (TenantModels + RLS): Conversation(user_id, dataset_id UUID, title, last_message_at), Message(conversation, role user|assistant,
   content, status pending|ok|error|clarification|blocked, error_code, sql_raw, sql_executed, sql_source, result JSON (capped), chart JSON,
   insights JSON, assumptions JSON, query_audit_id, feedback), BiQueryAudit (APPEND-ONLY via MakeAppendOnly: occurred_at, user_id, dataset_id,
   conversation_id, message_id, question, source cache_exact|cache_semantic|builder|llm|manual|none, raw_sql, executed_sql, params JSON,
   verdict ok|rejected|error|blocked, reject_code, policy_fingerprint, executing_role, plan_cost, duration_ms, row_count, bytes, ai_request_ids),
   SavedQuery. Orchestration service apps/bi/services.py ask(...) as a GENERATOR of (event, data) tuples, used by both the SSE and JSON paths:
     1 authorize: perm "bi.chat" + apps.governance.services.check_dataset_access(user_id, dataset_id, "query"); unknown/invisible dataset → 404.
     2 semantics = apps.semantic.services.dataset_semantics(dataset_id) -> nexora.contracts.DatasetSemantics (Data Engineering builds it THIS
       wave in parallel — code against exactly that name/contract and test with monkeypatched fakes).
     3 policy = apps.governance.services.effective_policy(user_id, dataset_id, semantics.tables, semantics.columns_by_table, privilege="query").
     4 ctx = QuestionContext(... policy, history = last 5 turns WITHOUT values (question, sql, column names only), columns_meta filtered to
       policy.allowed_columns, metrics, dataset_name, dataset_classification).
     5 proposal = apps.nl2sql.services.propose(question, ctx). blocked_reason → error event with that code (ai_policy_blocked /
       ai_quota_exhausted / ai_not_configured) + friendly detail; clarification → assistant message status clarification.
     6 guard = apps.sqlguard.services.validate(proposal.sql, policy) (Data Engineering, parallel; contract in nexora/contracts.py SqlGuard).
       If rejected and reject_code in REPAIRABLE_REJECT_CODES → ONE apps.nl2sql.services.repair(question, bad_sql, reject_code, ctx), re-validate.
       Rejections are audited (verdict rejected) and returned as error {code: reject_code}.
     7 rs = apps.sqlguard.services.execute(guard, policy, QueryTarget(kind="warehouse", org_id=...)).
     8 chart: proposal.chart_hint or a heuristic (1 date/time col + numeric → line; 1 category + numeric → bar (pie if <=6 & share-like);
       single value → kpi; else table). insights = apps.nl2sql.insights.compute_insights(...) + narrate_template(...) (local, no LLM).
     9 audit row, then apps.nl2sql.services.remember(...) after successful execution (see AI/ML w1 handoff for exact signatures).
     Emit SSE events exactly per CONTRACT: status{stage}, sql{raw_sql, executed_sql, source, query_audit_id}, result{columns, rows, row_count,
     truncated, chart}, insight{kind,text}, done{message_id}, error{code, detail}.
   Endpoints: conversations list/create/get/delete (owner = user; others 404), messages list, POST messages (Accept: text/event-stream →
   streaming response; otherwise synchronous JSON Message). IMPORTANT for SSE: do NOT hold the request DB transaction open while streaming —
   run the generator's DB work in short apps.core.tenancy.context.tenant_context(org_id) blocks; keep-alive comments; X-Accel-Buffering: no.
   POST /bi/queries manual SQL (perm "bi.sql") through the SAME guard+execute+audit path (source manual). GET /bi/query-history (perm audit.view
   sees all users, else own). POST /bi/messages/{id}/feedback {value: up|down} — up → nl2sql.services.store_verified(...).
   POST /exports {message_id, format csv|xlsx} → re-executes the audited executed_sql via the guard (never trusts stored rows), streams the file,
   escapes formula-injection cells (= + - @ tab CR prefix → prefix with '), perm exports.create, audited. GET /insights for the dashboard
   (latest insights from recent assistant messages of datasets the user can see). Saved queries CRUD.
   Tests: full API + service tests with fakes for semantic/sqlguard/nl2sql (pure) — event order, repair-once, rejection audit, policy-blocked,
   cross-user/cross-org 404s, export escaping, SSE response headers.
D. governance: add access history if cheap (AccessHistory append-only row per dataset query, written by bi) — optional.
