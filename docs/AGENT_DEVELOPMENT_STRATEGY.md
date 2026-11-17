# Agent Development Strategy (parallel development)

> Status: **Design.** Last updated 2026-11-17. Machine-readable ownership: `coordination/OWNERSHIP.md`.
> **The strategy:** contract-first, ownership-based work scheduled in waves. **The user approves at the end of every wave.** No agent merges, pushes or changes shared files.

## 1. Roles

There are five implementation agents plus the Integrator.

| Agent | Who | Writes | Main responsibilities |
|---|---|---|---|
| **Main Architect / Integrator** | The main Claude Code session, gated by you | Shared files only (§3) | Writes contracts before each wave; installs wave dependencies; launches waves; merges branches in order; runs full CI and the live Gemini smoke test; updates `FEATURE_STATUS.md` (only on green AC tests); presents wave reports |
| **Backend** | Agent | `backend/apps/{core,accounts,orgs,governance,bi,platform_admin,automation}/**` | Tenancy primitives; auth and RBAC; audit; jobs and SSE; governance policies (`effective_policy`); BI orchestration (chat SSE, dashboards, exports); automation engine (P10); platform admin (P11) |
| **Data Engineering** | Agent (two instances possible in P6 ∥ P7) | `backend/apps/{connectors,datasets,pipelines,quality,lineage,warehouse,semantic,sqlguard,customers,products}/**` | Ingestion; profiling; quality; pipelines; lineage; warehouse provisioning; semantic layer; **SQL guard and executor**; Customer 360 and Product 360 data logic |
| **AI/ML** | Agent | `backend/apps/{ai,nl2sql,documents,ml}/**` | `GeminiService`; prompt registry; quota and breaker; **`AIDataPolicy` gate**; local embeddings; NL→SQL resolution; insights; documents and RAG; ML workspace |
| **Frontend** | Agent | `frontend/**`, including its own `package.json` and lockfile | Design system; all screens; generated API client; SSE client |
| **QA/Security** | Agent | `backend/tests/{isolation,security,ai_eval,integration}/**`, `backend/testing/**`, `testdata/**`, `e2e/**`, `perf/**` | Isolation suite; SQL attack corpus and fuzzing; AI evaluation sets; upload fuzzing; E2E; performance; **independent security review** of `core.tenancy`, `sqlguard`, `ai.policy`, `core.crypto` and `connectors` SSRF. **Never edits app code.** Defects go into its handoff file for the owning agent. |

Unit tests inside an app (`backend/apps/<x>/tests/`) belong to that app's owner. Cross-cutting suites belong to QA.

## 2. Rules that prevent concurrent edits to shared files

1. **One owner per file per wave.** Globs are in `coordination/OWNERSHIP.md`, and every repository path matches exactly one owner.
2. **The ownership unit is the Django app.** Models, migrations, services, views, URLs, serializers, tasks and unit tests of an app belong to one agent, so migration numbering and `models.py` never conflict.
3. **Each agent works in its own git worktree,** on branch `agent/<role>/w<N>[-b<batch>]`. Only the Integrator merges.
4. **Ownership check in CI:** `scripts/check_ownership.py --agent <role> --base <wave-base>` fails on any changed path outside the agent's globs.
5. **Shared files are Integrator-only:**
   - `backend/nexora/**` (settings, root `urls.py`, `asgi.py`, `celery.py`);
   - `backend/pyproject.toml` and `backend/uv.lock`, `backend/conftest.py`;
   - `docker-compose*.yml`, `infra/**`, `.github/**`, `.env.example`;
   - `contracts/**`, `docs/**`, `coordination/OWNERSHIP.md`, `scripts/**`, root dotfiles, `README.md`.
6. **Pre-wiring in Phase 1** keeps shared files stable during waves:
   - every planned Django app is created and registered in `INSTALLED_APPS`;
   - root URLs include every app's `urls.py`;
   - Celery queues and routes are declared;
   - every env var is listed in `.env.example`;
   - each wave's Python dependencies are installed by the Integrator before the wave.
7. **Change requests instead of edits.** An agent that needs a shared change (dependency, setting, env var, contract change, FK into another agent's app, new permission codename) writes it under **Requests** in its own handoff file `coordination/handoffs/w<N>/<agent>.md`. Per-agent files mean no contention. The Integrator applies requests at a mid-wave checkpoint or between waves.
8. **Cross-app imports only through contracts.** Use `apps/<x>/services.py`, which implements `contracts/python/*` Protocols. Importing `models` is allowed only for FKs. import-linter enforces the layering in `SYSTEM_ARCHITECTURE.md` §3.1.
9. **Isolated test resources.** One shared infra stack (Integrator) serves everyone. Each agent uses its own test DB (`test_nexora_<agent>`), Redis DB index and MinIO prefix.
10. **Gemini quota is shared per Google project.**
    - Agents use `FakeGeminiService` and cassettes.
    - Only the AI/ML agent records cassettes, under `DEV_AGENT_RPD`.
    - Only the Integrator runs live smoke tests.
    - **No agent sends non-synthetic data to Gemini.**

## 3. Contracts (written by the Integrator, frozen during a wave)

| Contract | File(s) | Producer → consumers | Drift detection |
|---|---|---|---|
| REST API | `contracts/openapi.yaml` (versioned per wave) | Backend, Data Eng and AI/ML implement → Frontend, QA consume | CI: drf-spectacular output diffed with oasdiff; breaking or undeclared changes fail |
| SSE events | `contracts/events/*.schema.json` | Backend (`core.jobs`, `bi`) → Frontend, QA | JSON Schema validation in API tests and in the frontend zod guards |
| Python services | `contracts/python/{tenancy,policy,guard,ai,nl2sql,jobs}.py` (Protocols + frozen dataclasses: `QueryPolicy`, `GuardResult`, `ResultSet`, `SqlProposal`, `JobEvent`, `PayloadClass`, `AIDataPolicy`, `TENANT_GUC`) | Each owner implements → other agents consume | mypy structural checks; contract tests in `backend/tests/integration/contracts/` |
| Database | `docs/DATABASE_SCHEMA.md` (per-app tables) | App owner | Migration review by the Integrator; schema snapshot test |
| Test fakes | `backend/testing/` (built from the Protocols) | QA → all agents | Fakes are tested against the Protocols |

**Mid-wave contract changes** need an Integrator decision. They are announced to all agents in the next batch prompt and recorded in `contracts/CHANGELOG.md`.

## 4. Integration protocol (per wave)
1. **Prepare** (Integrator): update the contracts, install dependencies, create the wave base commit, write each agent's batch brief (tasks, ownership globs, contract version, AC IDs in scope).
2. **Run:** agents work in their worktrees and finish with a handoff file containing: files changed, tests run with results, AC IDs covered, requests, open issues, known limitations.
3. **Merge order:** **Backend → Data Engineering → AI/ML → Frontend → QA.** Full CI runs after each merge: ownership check, contract diff, import-linter, unit, API, DB, **isolation**, **SQL security**, **AI policy**, integration.
4. **Integration fixes:** the Integrator fixes glue only in Integrator-owned files. Defects in agent-owned files go back to their owner in a short follow-up run.
5. **Report:** the Integrator updates `FEATURE_STATUS.md` and presents the wave report: what is Working, Prototype or Mock, which ACs pass or fail, the risks, and the next wave plan. **The next wave starts only after you approve.**

## 5. Execution mechanism: verified availability and fallback

### 5.1 Availability (verified 2026-11-17 in this session)
- **Available:** the Workflow tool (Dynamic Workflows) is exposed in this Claude Desktop Code session, so it is enabled for your account here. The subscription plan cannot be read directly; whether the tool is present in a session is the reliable signal. It is re-checked in W0 before W1.
- **Confirmed capabilities:**
  - `agent(…, { isolation: 'worktree' })` runs each agent in its own fresh git worktree;
  - concurrency is capped at min(16, CPUs − 2), and this session's size guideline is "medium" (under 10 agents);
  - each run needs your explicit opt-in ("use a workflow");
  - progress is visible in `/workflows`, and runs are resumable.
- **Constraints:**
  1. It needs a git repository. W0 runs `git init` plus an initial commit first.
  2. Workflow agents run headless, so you cannot steer them mid-run. They suit bounded tasks of hours, not multi-week streams.
  3. Unchanged worktrees are removed automatically. Where changed worktrees and branches end up must be confirmed with a **one-agent dry run in W0** before W1 relies on it.

### 5.2 Mode A: Dynamic Workflows (primary, if the W0 dry run passes)
- Each wave is split into **2–4 task batches**, each about one day of agent work. Each batch is **one workflow run** with one agent per active role (`isolation: 'worktree'`).
- Each prompt carries the role, ownership globs, contract version, tasks and AC IDs.
- Each agent returns a schema-validated handoff (files changed, tests, requests, issues), which is also written to its handoff file.
- A final QA stage in the same pipeline runs the ownership check and the suites on each branch.
- The Integrator merges per §4, and you approve before the next batch.

### 5.3 Mode B: parallel Claude Desktop Code sessions with isolated worktrees (fallback)
- **When to use it:** if Workflow is unavailable or the dry run fails, or for long-running work you want to steer interactively.
- **Launching:** the Integrator starts **one Desktop Code session per active role per wave** with its own worktree (`use_worktree: true`) and fresh context, on branch `agent/<role>/w<N>`.
- **Brief for each session:** role; ownership globs quoted from `OWNERSHIP.md`; contract version; wave tasks and AC IDs; handoff file path. It also states the rules: *edit only inside your globs; commit only on your branch; never merge, push or switch branches; never send non-synthetic data to Gemini*.
- **Concurrency:** at most 4–5 sessions at once. You can open, steer or stop each one from the sidebar or split view.
- **Reports:** sessions report when they finish a turn. **The Integrator checks each branch's actual diff and test results, not just the report**, before merging per §4 with your approval.

**Common to both modes:** the same five agent roles plus the Integrator, ownership globs, contracts, waves, merge order, CI gates and testing architecture. **Only the launcher differs.**

## 6. MVP wave plan

| Wave | Weeks | Backend | Data Engineering | AI/ML | Frontend | QA/Security |
|---|---|---|---|---|---|---|
| **W0** (Integrator only, serial) | 1–2 | — | — | — | — | — |
| **W1**: Phase 2 + foundations | 3–4 | **Tenancy primitives first** (days 1–3 → mid-wave **tenancy checkpoint** merge), then accounts, orgs, roles, invitations, API keys, AI-policy consent endpoints, audit + sealing, jobs and SSE | Pure Polars parsing, profiling and schema-detection engine; pure `sqlguard.validate` (sqlglot, no DB) | `GeminiService`, prompt registry, quota limiter + breaker, `AIDataPolicy` gate (against the Protocols), local embedding service, cassette recorder | Design system, `AppShell`, auth, org, settings and AI-policy screens (Prism mock) | `testdata` generators, shared fakes, isolation-suite framework, SQL attack corpus (TDD vs the guard Protocol), AI-policy test suite |
| **W2**: Phase 3 + Phase 4 prep + Phase 5 models | 5–8 | Governance models + `effective_policy`, access history, usage metering, retention models | Connectors (CSV, Excel, JSON, PG; SSRF + read-only check), uploads, datasets, warehouse provisioning and loader, pipelines, scheduling, retries, quality, lineage, semantic layer; guard secure-source rewrite, executor, EXPLAIN check | NL2SQL router, exact and semantic caches, deterministic builder, schema-card prompts, insights engine, eval harness | Sources, upload, dataset explorer, pipeline canvas, runs, quality, lineage, policy editors | Isolation tests for new endpoints, upload fuzzing, guard fuzzing, golden NL question set |
| **W3**: Phase 4 integration + Phase 5 | 9–11 | `bi` chat SSE orchestration (`nl2sql` → `sqlguard` → audit), saved queries, dashboards, exports, retention jobs, Org-Admin and BI dashboard APIs | 1M-row performance, result caps, external PG hardening | Template narration + optional Gemini narration (policy-gated), quota-status API, eval tuning to ≥ 80% | Ask page, transparency panel, visual builder, SQL editor, dashboards, governance screens, quota UX | Playwright E2E, policy-result matrix, k6 baseline, **security review sign-offs** |
| **W4**: Hardening | 12 | Fixes | Fixes | Fixes | Fixes | Full regression + AC coverage report |

- **W0 detail:** docs → **review pause** → `git init`, scaffold, pre-wiring, ownership check, CI, contracts v1, execution-mode dry run.
- **W4 detail:** the Integrator produces the release candidate and updates `FEATURE_STATUS`.

### 6.1 Serial constraints (cannot be parallelized)
- Scaffold, pre-wiring and contracts come before any agent starts.
- Tenancy primitives come before any tenant-owned model, hence the mid-W1 checkpoint.
- Warehouse provisioning comes before executor tests on real data.
- The end-to-end NL→SQL flow needs `governance` + `sqlguard` + `nl2sql` + `bi`, so it lands in W3.
- Live Gemini runs (cassette recording, golden evaluation) are serialized because quota is per project.
- Shared-file changes are Integrator-only and happen between waves or at checkpoints.

### 6.2 Estimate verdict
- **Sequential:** about 15 weeks.
- **Parallel target:** about 12 weeks (W0 2 + W1 2 + W2 4 + W3 3 + W4 1). That is a saving of roughly 20%, **not** five times faster, because:
  - **The critical path stays serial:** scaffold (2) → tenancy (≈1) → datasets and warehouse (≈3) → guard, executor and policy (≈2) → BI end-to-end (≈2) → hardening (1) ≈ 11 weeks.
  - **Review time is the real bottleneck.** You review and approve every wave; integration overhead is about 15–20%.
  - **The shared Gemini free-tier quota** serializes live AI testing.
  - **Laptop limits:** RAM and CPU for 4–5 agents running Docker-backed tests on Windows.
  - Contract changes cause rework.
- **Recommendation:** target 12 weeks, commit to 15. Use the buffer for hardening, not new scope.

### 6.3 After the MVP
- **One wave:** P8 (AI/ML + Frontend + QA) ∥ P13 (Integrator), then P9 (AI/ML) once P8 is merged.
- **Then:** P6 ∥ P7 with **two Data Engineering instances** (separate apps `customers` and `products`).
- **P10 is last.**
- **Six-month outlook:** P8 and P9 can reach Working if their ACs pass, P6 and P7 Prototype, and P10 stays Planned.

## 7. Handoff file template (`coordination/handoffs/w<N>/<agent>.md`)

```markdown
# Handoff — <agent> — wave <N> batch <b>
Branch: agent/<role>/w<N>-b<b>   Base: <sha>   Contract version: <v>
## Done
- <task> → files: … → tests: … (pass/fail)  → AC IDs: P3-AC2 (partial)
## Requests (Integrator)
- [dep] polars==x.y  | [setting] … | [contract] … | [fk] … | [permission] …
## Open issues / known limitations
## Defects found in others' code (QA only)
- <owner>: <path>:<line> — <description> — repro test: <path>
```
