# Feature Status

> Maintained by the Integrator. **A module becomes Working only when its acceptance tests pass on `main`, including the DB-backed ones.**
>
> **Statuses:**
> - **Working**: implemented, with tests passing.
> - **Prototype**: implemented, but DB/integration tests not yet executed, or limited scale.
> - **Mock**: UI or demo data only.
> - **Planned**: not built.
>
> **Last integration: wave 4 merged (2026-11-21). FIRST FULL RUN AGAINST THE REAL LOCAL POSTGRESQL 17:**
> - **Backend pytest:** **4,840 passed**, 22 failed, 2 errors, 8 skipped (down from 98 failures on the first real-DB run).
>   - Remaining failures are mostly test-side, assigned for the final wave:
>     - platform-console and retention tests read RLS tables outside a tenant context, or need `transaction=True` with the ops alias (QA);
>     - the SSRF obfuscated-loopback hosts ARE refused (400) but with error code `invalid` instead of `connector_host_invalid` (DE);
>     - C360/P360/ML permission tests and 2 ML security tests (DE/AI-ML/QA);
>     - 2 live-Postgres connector tests need a dedicated read-only source role (environment).
> - **Static checks:** ruff, `manage.py check`, `makemigrations --check`, import-linter (2/2) and the conventions check are green.
> - **Frontend:** lint, typecheck, 255/255 vitest and `next build` are green.
> - **Local runtime:** the app runs against the user's PostgreSQL (`readyz`: database, cache, storage fs, thread runner, scheduler all ok). All migrations are applied.

| Area | Module | Status | Evidence / notes |
|---|---|---|---|
| Foundation | Monorepo, Django project, apps pre-wired, CI workflow | Prototype | CI not run on GitHub (no remote yet, by user choice). |
| Foundation | One-command local PostgreSQL setup (`scripts/setup_local.py`; 6 lines in .env), local-runtime settings | Working | Ran successfully on the user's PostgreSQL 17.10 (2026-11-20): users + 2 DBs created, all migrations OK, warehouse provision test OK. |
| Foundation | Tenancy core (RLS `app.org_id`, fail-closed managers, `TenantAPIView`, `TenantTask`) | Prototype | All 6 W1 QA defects fixed (W2 Backend). Repro tests need the DB. |
| Foundation | Local runtime: thread task runner, DB job events + SSE (ASGI/WSGI), fs storage + signed links, local scheduler (advisory lock), DB cache, pgvector-optional vectors | Prototype | Unit tests pass. DB tests written, not run. |
| Auth/Orgs | Signup, login, lockout, MFA, orgs, members, invitations, roles, API keys (password re-auth) | Prototype | Unit tests pass. API tests need the DB. |
| AI policy | `synthetic_only` default, `schema_only` consent ledger, tier gate, payload-class gate | Prototype | Gate unit tests pass. Consent DB tests not run. |
| Governance | `effective_policy`, ACLs, column masks, row predicates, append-only AccessHistory | Prototype | Pure policy tests pass. API tests need the DB. |
| Data Eng | Upload (CSV/Excel/JSON), schema inference, profiling, PII, quality score, warehouse loader | Prototype | 505 owned pure tests pass. 74 DB tests not run. |
| Data Eng | Semantic layer (auto-generated models, metrics, `DatasetSemantics`) | Prototype | Generation unit-tested. API needs the DB. |
| Data Eng | SQL guard (validate/rewrite, masks, row predicates, EXPLAIN check) + read-only executor | Prototype | Corpus: everything passes except 9 `mask_null` oracle cases (approved rendering; QA updates the oracle in W3). Execute tests need the DB. |
| Data Eng | PostgreSQL connector (SSRF pinning, read-only role check, encrypted creds, introspect, import) | Prototype | 102 unit tests pass. 25 DB/live tests not run. |
| Data Eng | Pipelines (validated DAG, 13 Polars steps, safe derive, runner + retries, cron schedules, preview), lineage | Prototype | 127 unit tests pass. API/runner DB tests not run. |
| AI | Gemini service (quota, breaker, redaction, durable request log), `verify_gemini` command | Prototype | Unit tests with a fake transport. **No live call** (no API key yet). |
| AI | NL→SQL (exact/semantic cache, deterministic builder, Gemini proposal + repair), local insights | Prototype | 169 unit tests pass. Builder-only golden accuracy is 43/48 (89.6%). |
| BI | Conversational BI orchestration (chat SSE/JSON, guard, audit, manual SQL, history, feedback, exports, insights, saved queries) | Prototype | 44 unit tests with fakes. 29 API tests need the DB. |
| Documents | Upload safety, extraction + OCR (eng/hin/guj), chunking, local embeddings, hybrid search, policy-gated Q&A with citations, summaries | Prototype | 85 unit tests pass. 17 API tests not run. Tesseract binary optional. |
| UI | Design system, shell, auth, dashboard, datasets, settings, audit | Prototype | Builds. Not yet run against the live backend. |
| UI | Ask AI workspace, SQL editor, history; sources, pipeline builder, runs, lineage, quality, semantic; documents | Prototype | 120 vitest tests. Not yet run against the live backend. |
| W3 | Dashboards + widgets (viewer-policy refresh, sharing), notifications, AI quota UX, `seed_demo` (real ingestion), no-mock UI audit | Prototype | Unit and component tests pass. DB/e2e tests written, not run. |
| W4 | ML workspace (EDA, recommendations, sklearn/XGBoost/statsmodels training, SHAP, registry, promote, predict API with API keys, batch, PSI drift) | Prototype | Engine and API tests pass on the real DB, except 1 permissions test + 2 ML security tests (final wave). |
| W4 | Customer 360 (identity resolution, review queue, profiles, timeline, RFM segments) + Product 360 (normalize, match, corrections on approve, quality, search) | Prototype | DE: 1,092/1,092 owned DB tests pass. ER on testdata: customers F1 1.00; supplier products F1 0.90. |
| W4 | Governance UI, retention job, org usage, platform super-admin console | Prototype | Backend: 554/555 owned DB tests pass; platform QA tests need test-side fixes. |
| W5 | Customer 360, Product 360, document intelligence completion | Planned | |
| W6 | Workflow automation with approvals, usage analytics | Planned | |
| W7 | Hardening, performance, final manual setup/test guide | Planned | |

## Open integration issues (carried into W3)
1. **No database run yet.** Every DB-backed suite is unexecuted. This is the main risk until the user creates the local DB.
2. **SQL-corpus oracle.** The guard renders `mask_null` as `CASE WHEN FALSE THEN col END`. That form is approved: it is type-preserving and never reads the column. QA updates its oracle in W3.
3. **Isolation fixtures.** `test_every_resource_route_has_an_isolation_factory` lists routes in several apps without factories. Each owner adds them in W3.
4. **Live paths not exercised:** Gemini (no key yet), OCR (Tesseract optional), and the e5 embedding model (downloaded on first use; a W3 fallback is added).
