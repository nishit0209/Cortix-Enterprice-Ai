# Cortix Enterprise AI

Enterprise AI and data intelligence platform covering:
- data ingestion and ETL;
- data quality, conversational BI (natural language to guarded SQL), governance;
- Customer 360, Product 360, document intelligence, an ML workspace and workflow automation.

**Google Gemini is the only external generative-AI provider.** Embeddings and conventional ML run locally.

> **Start here:** [`docs/PRODUCT_OVERVIEW.md`](docs/PRODUCT_OVERVIEW.md) explains what the product is. [`docs/RUN_GUIDE.md`](docs/RUN_GUIDE.md) covers install, run, demo, testing and the production checklist. Locally it needs only **PostgreSQL**: Redis, MinIO and pgvector are optional.
>
> **Status:** The truth source for what works is [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md), which uses the statuses Working, Prototype, Mock and Planned.

## Stack
| Layer | Technology |
|---|---|
| Backend | Python 3.12, Django 5.2, DRF, Celery, Redis, SSE (ASGI/uvicorn) |
| Data | PostgreSQL 17 + pgvector (`nexora_app` metadata with RLS; `nexora_wh` warehouse, one schema per org), Polars, pandas, SQLGlot |
| AI | `google-genai` (Gemini only), local sentence-transformers (`intfloat/multilingual-e5-base`) |
| ML | scikit-learn, XGBoost, SHAP, statsmodels |
| Frontend | Next.js (App Router), TypeScript, Tailwind CSS, Framer Motion, Recharts, TanStack Query, React Flow |
| Infra | Docker Compose, MinIO, Caddy, Mailpit (dev), GitHub Actions |

## Quick start (Windows, macOS, Linux)
**Prerequisites:** Docker Desktop running, Python 3.12, Node 24.

1. Create the environment file:
   ```bash
   cp .env.example .env
   ```
   Then put your Gemini key in `.env` (`GEMINI_API_KEY=`). It is optional, and AI features degrade gracefully without it.
2. Start the backend stack (Postgres on 5433, Redis 6380, MinIO 9000/9001, Mailpit 8025, API 8000, Caddy 8080):
   ```bash
   docker compose up -d --build
   ```
3. Start the frontend:
   ```bash
   cd frontend && npm install && npm run dev
   ```
4. Open **http://localhost:8080** (Caddy: UI and API on the same origin), or http://localhost:3000 (Next.js proxies `/api`).

### Backend development on the host
```bash
python -m venv .venv
```
```bash
.venv/Scripts/pip install -r backend/requirements-dev.txt
```
```bash
cd backend && ../.venv/Scripts/pytest
```
- Tests run as the `nexora_app` role, so **Row-Level Security is enforced in tests**.
- When several agents run tests at once, set `PYTEST_DB_SUFFIX=<agent>` so each gets its own test database.

## Security model (summary)
- **Multi-tenancy:**
  - every tenant table has `org_id` plus FORCE RLS on the setting `app.org_id`, set transaction-locally;
  - managers fail closed when no tenant is set;
  - ingested data lives in one Postgres schema per org with a per-org read-only role.
- **NL→SQL:** SQLGlot validation → table/column allowlists → row and column policy rewrite → read-only execution with timeouts and row caps → audit.
- **AI data policy:**
  - the default is `synthetic_only`;
  - `schema_only` requires explicit org-admin consent;
  - `aggregates_only` and `full` require a paid Gemini configuration;
  - no fallback provider; quota-aware caching.

## Repository layout
`backend/` (Django: `nexora/` project, `apps/*` modules) · `frontend/` (Next.js) · `infra/` (Postgres init, Caddy) · `contracts/` (API contracts) · `coordination/` (agent ownership, handoffs) · `docs/` (architecture reference) · `scripts/` (CI checks) · `testdata/`, `e2e/`, `perf/` (QA).

## Parallel development
Built by an Integrator plus five agents with strict file ownership (`coordination/OWNERSHIP.md`):
- Backend
- Data Engineering
- AI/ML
- Frontend
- QA/Security

Each agent works in its own git worktree and branch. The Integrator merges and runs the full test suite.
