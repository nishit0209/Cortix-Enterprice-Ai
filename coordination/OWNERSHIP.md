# Ownership Map

> Read by `scripts/check_ownership.py` (to be created in Phase 1). Every repository path must match **exactly one** owner.
> **Matching order:** the first matching row wins, so specific globs come before general ones. Unmatched paths fail CI.
> Changes to this file are Integrator-only. Design rationale: `docs/AGENT_DEVELOPMENT_STRATEGY.md`.

| # | Glob | Owner |
|---|---|---|
| 1 | `backend/apps/core/**` | backend |
| 2 | `backend/apps/accounts/**` | backend |
| 3 | `backend/apps/orgs/**` | backend |
| 4 | `backend/apps/governance/**` | backend |
| 5 | `backend/apps/bi/**` | backend |
| 6 | `backend/apps/platform_admin/**` | backend |
| 7 | `backend/apps/automation/**` | backend |
| 8 | `backend/apps/connectors/**` | data_engineering |
| 9 | `backend/apps/datasets/**` | data_engineering |
| 10 | `backend/apps/pipelines/**` | data_engineering |
| 11 | `backend/apps/quality/**` | data_engineering |
| 12 | `backend/apps/lineage/**` | data_engineering |
| 13 | `backend/apps/warehouse/**` | data_engineering |
| 14 | `backend/apps/semantic/**` | data_engineering |
| 15 | `backend/apps/sqlguard/**` | data_engineering |
| 16 | `backend/apps/customers/**` | data_engineering |
| 17 | `backend/apps/products/**` | data_engineering |
| 18 | `backend/apps/ai/**` | ai_ml |
| 19 | `backend/apps/nl2sql/**` | ai_ml |
| 20 | `backend/apps/documents/**` | ai_ml |
| 21 | `backend/apps/ml/**` | ai_ml |
| 22 | `backend/tests/isolation/**` | qa_security |
| 23 | `backend/tests/security/**` | qa_security |
| 24 | `backend/tests/ai_eval/**` | qa_security |
| 25 | `backend/tests/integration/**` | qa_security |
| 26 | `backend/testing/**` | qa_security |
| 27 | `testdata/**` | qa_security |
| 28 | `e2e/**` | qa_security |
| 29 | `perf/**` | qa_security |
| 290 | `backend/tests/FINAL_VERIFICATION.md` | qa_security |
| 30 | `frontend/**` | frontend |
| 31 | `coordination/handoffs/*/backend.md` | backend |
| 32 | `coordination/handoffs/*/data_engineering.md` | data_engineering |
| 320 | `coordination/handoffs/*/data_pipelines.md` | data_engineering |
| 33 | `coordination/handoffs/*/ai_ml.md` | ai_ml |
| 34 | `coordination/handoffs/*/frontend.md` | frontend |
| 35 | `coordination/handoffs/*/qa_security.md` | qa_security |
| 36 | `backend/nexora/**` | integrator |
| 37 | `backend/pyproject.toml` | integrator |
| 38 | `backend/uv.lock` | integrator |
| 39 | `backend/conftest.py` | integrator |
| 40 | `backend/manage.py` | integrator |
| 41 | `contracts/**` | integrator |
| 42 | `docs/**` | integrator |
| 43 | `coordination/**` | integrator |
| 44 | `infra/**` | integrator |
| 45 | `scripts/**` | integrator |
| 46 | `.github/**` | integrator |
| 47 | `docker-compose*.yml` | integrator |
| 48 | `.env.example` | integrator |
| 49 | `*` (root files: `README.md`, `.gitignore`, `.editorconfig`, `.pre-commit-config.yaml`, …) | integrator |

## Notes
- **Exception:** `backend/apps/ai/tests/cassettes/**`, if used, stays under `ai_ml` via row 18. Shared golden cassettes live under `backend/tests/ai_eval/cassettes/` (QA, row 24). Only the AI/ML agent *records* them, and it delivers them to QA through its handoff.
- **Second agent instances** (for example two Data Engineering agents for P6 ∥ P7) split by app: `customers/**` → `data_engineering_a`, `products/**` → `data_engineering_b`. The split is declared for that wave in this file.
- **Ownership changes** take effect only at a wave boundary.
