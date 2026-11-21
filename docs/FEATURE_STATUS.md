# Feature Status

> Maintained by the Integrator. **Working** = implemented, with its automated tests passing against a real PostgreSQL database.
> - **Prototype**: implemented, but a live dependency is not yet exercised.
> - **Planned**: not built.

## Final integration (2026-11-21): all waves (W1–W5 + final fixes) merged on `main`

| Check | Result |
|---|---|
| Backend test suite on the real local PostgreSQL 17 | **5,219 passed, 0 failed, 0 errors, 8 skipped** |
| Frontend | lint ✓, typecheck ✓, **288/288** vitest ✓, `next build` ✓ |
| Static checks | ruff ✓, `manage.py check` ✓, `makemigrations --check` ✓, import-linter 2/2 ✓, conventions ✓, ownership ✓ |
| Production profile | `manage.py check --deploy` with `nexora.settings.prod`: only the optional HSTS-preload notice |
| Local runtime | runs with PostgreSQL only. `readyz`: database, cache, fs storage, thread runner, scheduler all ok |

| Module | Status | Notes |
|---|---|---|
| Foundation: multi-tenancy (RLS), auth, MFA, orgs, roles, API keys, audit, jobs/SSE, notifications | **Working** | Isolation matrix covers every tenant route: cross-org 404 and anonymous 401 |
| Local runtime: one-command setup, thread task runner, DB job events, fs storage + signed links, scheduler | **Working** | `scripts/setup_local.py` was verified on the user's PostgreSQL 17.10 |
| A. Dashboard: KPIs, custom dashboards, pinned AI answers, insights | **Working** | Widgets re-run under each viewer's own policy |
| B. Data Engineering Hub: uploads, PostgreSQL connector, profiling/PII, quality, pipelines, lineage, semantic layer | **Working** | Includes SSRF and credential-security suites. 1M-row ingest perf test available |
| C. Conversational BI: NL→SQL, SQL guard, streaming, history, exports, saved queries | **Working** for the cache and builder paths | SQL attack corpus fully green. Builder-only golden accuracy ≥ 45/48. **Gemini LLM path: Prototype** until it runs with a live key (`verify_gemini`, `record_cassettes`) |
| D. Customer 360: identity resolution, review queue, profiles, timeline, segments, churn/CLV scoring, NBA | **Working** | ER on testdata: precision 1.00, recall 1.00 |
| E. Product 360: normalize, match, corrections on approve, quality, search | **Working** | Supplier matching F1 0.90 (only price separates the remaining look-alikes) |
| F. Document Intelligence: extraction, OCR, chunking, hybrid search, cited Q&A, summaries, extract/compare/collections | **Working** (local paths) | OCR needs the Tesseract binary. Semantic search needs `manage.py download_models` and otherwise falls back to full text. Generative answers depend on the Gemini key and policy |
| G. ML Workspace: EDA, recommendations, training, SHAP, registry, approval-gated promotion, predict API, batch, drift | **Working** | sklearn-oracle metric checks, determinism, artifact sha256 checks, API-key scope tests |
| H. Automation: workflows, triggers (schedule / HMAC webhook / events), approvals, agents | **Working** | High-impact steps are always approval-gated. Webhook replay/forgery tests |
| I. Governance: ACLs, column masks, row policies, preview, retention, access history, AI data policy | **Working** | |
| J. Admin console: org admin, usage analytics, platform super-admin (suspend/reactivate, health, jobs, logs) | **Working** | Platform payloads verified to carry no tenant rows |
| Frontend (all screens above) | **Working** (unit/build) | Playwright e2e specs exist (`e2e/`). Run them against the live stack (`E2E_SKIP_IF_DOWN`) |

## Not yet verified (honest list)
1. **Live Gemini calls.** All AI tests use fakes and recorded responses. Run `manage.py verify_gemini` with your key, then try Ask AI questions that the builder declines.
2. **Playwright end-to-end in a real browser** and the **k6 load test** (`perf/`). Not run in this session.
3. **Production deployment.** Docker/Caddy/HTTPS has not been exercised yet; it is planned for the GitHub/Docker step. See `docs/RUN_GUIDE.md` Part 6.
4. **Scale beyond one machine** (Redis + Celery workers, S3, pgvector) is supported by configuration but was not load-tested.
