# Nexora Enterprise AI: Design Documents

> Written 2026-11-17 as the first deliverable after the plan was approved. **No application code exists yet.** Implementation starts only after these documents are reviewed and approved.

Nexora is a multi-tenant Enterprise AI and Data Intelligence Platform: data engineering, conversational BI, governance and, later, Customer 360, Product 360, Document Intelligence, ML and governed automation. **Google Gemini is the only external generative-AI provider.**

## Reading order

| # | Document | What it covers |
|---|---|---|
| 1 | [PROJECT_VISION.md](PROJECT_VISION.md) | Problems, personas, MVP scope, non-goals, success metrics, status legend |
| 2 | [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) | Modular monolith, processes, app map and layering, technology choices, decision log |
| 3 | [MULTI_TENANCY_DESIGN.md](MULTI_TENANCY_DESIGN.md) | Tenancy options compared, hybrid decision, RLS on `app.org_id`, isolation tests |
| 4 | [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) | Threat model, authentication and authorization, SQL guard, secrets, OWASP mapping |
| 5 | [AI_ARCHITECTURE.md](AI_ARCHITECTURE.md) | Verified Gemini facts, `GeminiService`, quota strategy, `AIDataPolicy`, NL→SQL, RAG, agents |
| 6 | [DATA_ENGINEERING_DESIGN.md](DATA_ENGINEERING_DESIGN.md) | Connectors, ingestion, profiling, quality, pipelines, lineage |
| 7 | [ML_ARCHITECTURE.md](ML_ARCHITECTURE.md) | ML workspace, model recommendation, registry, prediction API, monitoring |
| 8 | [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | Every table, constraint, index, RLS policy and DB role |
| 9 | [DATABASE_ERD.md](DATABASE_ERD.md) | ER diagrams per domain |
| 10 | [API_SPECIFICATION.md](API_SPECIFICATION.md) | REST and SSE conventions, endpoints per phase, Python service contracts |
| 11 | [UI_UX_SPECIFICATION.md](UI_UX_SPECIFICATION.md) | Design system, shell, key screens, persona dashboards |
| 12 | [TESTING_STRATEGY.md](TESTING_STRATEGY.md) | Test pyramid, isolation and SQL-security suites, AI evaluation, CI |
| 13 | [DEPLOYMENT_ARCHITECTURE.md](DEPLOYMENT_ARCHITECTURE.md) | Compose topology, CI/CD, backups, observability, paid services |
| 14 | [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md) | All 13 phases with objectives, migrations, endpoints, acceptance criteria and risks |
| + | [AGENT_DEVELOPMENT_STRATEGY.md](AGENT_DEVELOPMENT_STRATEGY.md) | Parallel agent waves, ownership, contracts, execution modes |
| + | [FEATURE_STATUS.md](FEATURE_STATUS.md) | Live status matrix (everything is Planned today) |
| + | [../coordination/OWNERSHIP.md](../coordination/OWNERSHIP.md) | Machine-readable file ownership |

## Key decisions at a glance
- **Architecture:** a Django + DRF modular monolith (ASGI/SSE), Celery + Redis, PostgreSQL 17 + pgvector, a Next.js frontend, and Docker Compose everywhere (including Windows development).
- **Tenancy:**
  - metadata lives in shared tables with `org_id` and FORCE RLS, using a single setting name, `app.org_id`, set transaction-locally;
  - ingested data gets one schema per org, with a per-org read-only role.
- **AI:**
  - Gemini only, via `google-genai`, with model IDs taken from configuration and no fallback provider.
  - **`AIDataPolicy` defaults to `synthetic_only`.** `schema_only` requires explicit org Owner/Admin consent. `aggregates_only` and `full` are refused on the free tier.
- **Embeddings:** local multilingual (`intfloat/multilingual-e5-base`, with `BAAI/bge-m3` evaluated in Phase 8) for English, Hindi and Gujarati.
- **NL→SQL safety:** sqlglot validation → secure-source rewrite (row and column policies) → EXPLAIN check → read-only per-org role with timeouts and caps → audit.
- **Delivery:**
  - The MVP is Phases 1–5 plus basic dashboards and the security baseline: about 15 weeks sequential, with a target of about 12 weeks using parallel agent waves.
  - The full scope is about 40–45 weeks for one developer.
