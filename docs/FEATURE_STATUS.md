# Feature Status

> Maintained by the Integrator. **A module becomes Working only when its acceptance tests pass on `main`.**
> Statuses:
> - **Working**: implemented and tested.
> - **Prototype**: implemented, but with limited tests or scale.
> - **Mock**: UI or demo data only.
> - **Planned**: not built.
>
> **Last integration: wave 1 merged (2026-11-17).** On `main`:
> - Backend non-DB tests: 890 passed, 122 skipped (waiting on W2 features), 5 strict-xfail (known defects).
> - 2 failures: one needs the database; the other is `test_tenant_unique_constraints_include_org`, a test-allowlist issue (see below).
> - **708 DB-backed tests have not run**, because Docker/Postgres was not available on the dev machine.
> - Frontend: typecheck, lint, 48/48 unit tests and `next build` are all green.
>
> No module below is **Working** until its DB-backed tests pass.

| Area | Module | Status | Evidence / notes |
|---|---|---|---|
| Foundation | Monorepo, Docker Compose, Django project, all apps pre-wired, Celery queues | Prototype | Compose not yet verified running (Docker not running) |
| Foundation | Tenancy core (`app.org_id` RLS, fail-closed managers, `TenantAPIView`, `TenantTask`) | Prototype | QA found 4 defects (open, W2 Backend): `MakeAppendOnly` `%` placeholder blocks `migrate`; GUC not restored after a nested `tenant_context`; actor leak in `TenantTask`; rollback poisons an outer transaction. |
| Foundation | Audit log, jobs + SSE (Redis Streams), object storage helpers | Prototype | QA found 2 storage defects (open): path traversal in the key prefix check; Content-Disposition injection. |
| Foundation | CI (conventions, ownership, ruff, import-linter, pytest, frontend, gitleaks) | Prototype | Workflow written; not run on GitHub (no remote) |
| W1 | Auth (signup, login, lockout, sessions, CSRF, TOTP MFA), orgs, members, invitations, roles, API keys | Prototype | Backend. 13 unit tests pass; 64 API tests written, not yet run (no DB). |
| W1 | AI data policy (default `synthetic_only`, consent ledger for `schema_only`, tier gate, revoke) | Prototype | Backend + AI/ML. 306 policy-gate tests pass; 20 consent API tests not yet run (no DB). |
| W1 | Governance policy engine (`effective_policy`, ACL, column masks, row predicates) | Prototype | Backend. 44 pure policy-compute tests pass; 19 API tests not yet run. |
| W1 | File ingestion (CSV, Excel, JSON), schema/type inference, profiling, PII detection | Prototype | Data Engineering. 203 pure tests pass; 21 API tests not yet run. |
| W1 | Quality rules + score, duplicate detection, warehouse loader (schema per org), lineage | Prototype | Data Engineering. Engine tests pass; 26 DB tests not yet run. |
| W1 | Gemini service (quota limiter, circuit breaker, redaction, request log, policy gate) | Prototype | AI/ML. Unit tests pass with a fake transport; **no live Gemini call made** (no API key). |
| W1 | Local multilingual embeddings (`multilingual-e5-base`) | Prototype | Interface tested with a fake model; model not downloaded or run. |
| W1 | NL→SQL engine (exact and semantic cache, deterministic builder, Gemini proposal + repair), local insights | Prototype | AI/ML. 172 unit tests pass; 7 pgvector cache tests not yet run. |
| W2 | PostgreSQL connector, pipelines, scheduler, semantic layer, SQL guard + executor | Planned | Wave 2 |
| W3 | Conversational BI end to end (chat SSE, charts, exports, query history) | Planned | Wave 3. Needs `GEMINI_API_KEY` for the live LLM path. |
| W4 | Governance UI, analytics, admin console | Planned | |
| W5 | Customer 360, Product 360, Document Intelligence | Planned | |
| W6 | ML workspace, workflow automation | Planned | |
| W7 | Hardening, performance, production readiness | Planned | |
| UI | Design system, app shell (sidebar, org switcher, command menu, notifications, themes), auth screens, dashboard, datasets, AI-policy + members settings, audit | Prototype | Frontend. 48 unit tests, typecheck, lint and build are green; **not yet exercised against the real backend**. |
| QA | Isolation suite, SQL attack corpus + fuzzing, AI-policy tests, upload/secret/storage tests, synthetic testdata, Playwright + k6 scaffolds | Prototype | QA. 122 SQL-guard tests skip until `sqlguard.validate` lands (W2). |

## Open integration issues (carried into wave 2)
1. **Unique tokens across orgs.** `test_tenant_unique_constraints_include_org` flags `ApiKey.prefix` and `Invitation.token_hash`. These are intentionally unique across all orgs, because they are looked up before the tenant is known via SECURITY DEFINER functions. QA should add an explicit allowlist.
2. **Fixes owned by Backend:** the six core defects above. `MakeAppendOnly` is a blocker for every DB test.
3. **Asset classifier (Data Engineering).** Register `register_asset_classifier('dataset', …)`, `register_asset_exists` and `register_dataset_schema_provider`.
4. **Reject codes (Data Engineering → AI/ML).** Align the `sqlguard` reject codes with the nl2sql `REPAIRABLE_CODES` and SECURITY_ARCHITECTURE §5.1.
5. **AI request log (AI/ML).** Write `AIRequestLog` through the new `durable` DB alias.
