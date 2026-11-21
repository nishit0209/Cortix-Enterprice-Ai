# Nexora Enterprise AI — Architecture & Implementation Plan

## Context

Nexora is a multi-tenant Enterprise AI & Data Intelligence Platform. Organizations connect data sources, clean and govern their data, ask business questions in natural language, and later add Customer 360, Product 360, document intelligence, ML and AI workflow agents. Google Gemini is the only external generative-AI provider, and the free tier comes first.

The request is for an architecture and a phased roadmap. **No application code is written until approved.** The first approved step writes 14 design documents into `docs/` and then **pauses for review**.

### Decisions already made with the user
| Topic | Decision |
|---|---|
| Builder / timeline | Solo developer (with Claude Code), about 4–6 months. Estimates below are sized to this. |
| Data in AI calls | **Synthetic or public data only** while on the Gemini free tier. The per-org `AIDataPolicy` is `synthetic_only` **(default everywhere)**, `schema_only` (requires explicit org-admin consent), `aggregates_only` or `full`. Only the first two are allowed on the free tier (§6). No real customer records or confidential data go to Gemini until that org has a paid Gemini key or Vertex (still Gemini only). |
| Embeddings | **Local open-source multilingual models by default** (English, Hindi, Gujarati), CPU-friendly, so no document text leaves the server. Gemini embeddings are an optional future setting. |
| First step after approval | Write the 14 docs, then stop for review. |

---

## 1. Repository inspection findings

- `D:\nexora enterprice ai` is **empty**: no code, no git repo, no framework, no dependencies. This is a greenfield build, so nothing will be overwritten.
- Local toolchain: Python 3.12.7, Node 24.21, npm 11.19, Docker 29.8, PostgreSQL 17.10 (local), git 2.49. Redis, pnpm and uv are not installed.
- **Windows constraint:** Celery does not officially support Windows. The whole backend stack (API, workers, beat, Redis, Postgres+pgvector, MinIO) will run in **Docker Compose**. The frontend can run natively or in Compose. Plain PostgreSQL 17 on Windows has no pgvector build, so use the `pgvector/pgvector:pg17` image.
- Missing infrastructure, all to be created: monorepo layout, Compose stack, Django project, Next.js app, CI, design system, the Gemini service, test harness, `.env.example`, and git init.

## 2. Gemini verification (official docs, read 2026-11-17)

Google's docs now redirect to `aistudio.google.com/docs/*`. Facts that shape the design:

- **SDK:** `google-genai` (Python). The old `google-generativeai` package is deprecated (since 2025-11-30), so do not use it.
- **Free-tier text models (stable):** `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` (shutdown 2027-05-07). `gemini-4-hydrogen` is also listed as free. `gemini-4-carbon` and `gemini-4-argon` are paid only.
  - `gemini-2.5-pro` and `gemini-2.5-flash` shut down 2026-12-01. **Do not build on 2.x models.**
- **Embeddings:** `gemini-embedding-2` is current and listed as free. `gemini-embedding-001` shuts down 2028-05-14. This only matters for the optional Gemini embedding mode.
- **Quotas:** per-model free RPM/TPM/RPD numbers now appear **only in AI Studio**. Developer forums report about 20 RPD for Flash and a few hundred RPD for Flash-Lite, with cuts made without notice.
  - Limits apply **per project, not per API key**.
  - The daily quota resets at **midnight Pacific**.
  - Exhaustion returns HTTP **429 `RESOURCE_EXHAUSTED`**.
  - **Batch API is not available on the free tier.** Context caching is free only on some models.
  - **Implication:** the free tier supports development and demos, not real multi-user production. The design must treat each Gemini call as a scarce resource.
- **Data use on the free tier:** prompts and outputs may be used to improve Google products, and human reviewers may read them. The terms say "Do not submit sensitive, confidential, or personal information." This is why the synthetic-data decision above exists.
  - Users in the **EEA, Switzerland or UK** may only be served with **paid** services.
  - The terms require business use and users aged 18+.
- **Model IDs come from configuration, never code:** `GEMINI_MODEL_FAST`, `GEMINI_MODEL_REASONING`, `GEMINI_MODEL_VISION`, `EMBEDDING_PROVIDER`.
  - Initial defaults: FAST = `gemini-3.5-flash-lite`, REASONING = `gemini-3.8-flash`.
  - A startup check calls `models.list` and fails loudly if a configured model is missing or deprecated.
- **Still to verify in Phase 1** by calling the API with a dev key, because the docs don't state it for the free tier: structured output with a JSON schema, function calling, streaming support, and the actual RPD shown in AI Studio for the project.

---

## 3. Recommended architecture: modular monolith

No microservices at first. There is one Django codebase with strict app boundaries, run as separate processes:

```
                 ┌──────────────┐
 Browser ──────► │ Caddy (TLS,  │──► Next.js (web, :3000)
                 │ same-origin) │──► Django ASGI API (/api, SSE) ── uvicorn
                 └──────────────┘            │
                                             ├─► Postgres "nexora_app"  (metadata, pgvector, RLS)
                                             ├─► Postgres "nexora_wh"   (ingested data, schema-per-org)
                                             ├─► Redis (Celery broker, cache, rate limits)
                                             ├─► MinIO / S3 (uploads, artifacts, exports)
                                             └─► Celery workers: ingest | ai | ml | default  + beat
                                                        └─► Gemini API (only via apps.ai GeminiService)
```

### Stack (chosen for need, not fashion)
| Layer | Choice | Why |
|---|---|---|
| Backend | Django 5.x + DRF + drf-spectacular (OpenAPI) + django-filter, served with **uvicorn (ASGI)** | Mature ORM and migrations, permissions model, admin for super-admin back-office. Async views for **SSE** streaming. |
| Auth | Django sessions in **httpOnly cookies**, same-origin behind Caddy; CSRF on; argon2 hashing; TOTP MFA (django-otp); hashed API keys for the programmatic Prediction API | Avoids JWTs in localStorage. OIDC/SAML SSO is planned for later. |
| Data | pandas + **Polars** (profiling and transforms), pyarrow, `python-calamine` / openpyxl (Excel), SQLAlchemy 2 (external DB introspection), **sqlglot** (SQL parsing and rewriting) | |
| Async | Celery 5 + Redis, django-celery-beat (schedules), separate queues per workload | |
| AI | `google-genai` behind one `GeminiService` | |
| Embeddings | sentence-transformers running on CPU | |
| ML | scikit-learn, XGBoost, SHAP, statsmodels (forecasting), joblib artifacts stored in S3 | PyTorch only if a later need justifies it. |
| Entity resolution | rapidfuzz plus Splink (DuckDB backend) | |
| Documents | pdfplumber / pypdf, python-docx, openpyxl; **Tesseract OCR with `eng`, `hin` and `guj` language packs** | Avoid PyMuPDF because of its AGPL licence. |
| Frontend | Next.js (App Router) + TypeScript + Tailwind + Radix primitives (shadcn-style, restyled with our own design tokens) + TanStack Query/Table + Recharts + **@xyflow/react** (pipeline and workflow canvases) + Framer Motion + next-themes + zod + react-hook-form | |
| Testing | pytest, pytest-django, factory_boy, Hypothesis (SQL-guard fuzzing), Vitest, Playwright, k6 | |
| Infra | Docker Compose (dev and single-VM prod), GitHub Actions CI, MinIO in dev and any S3-compatible store in prod, Prometheus + Grafana, Sentry (self-hosted or free tier) | |

### Monorepo layout
```
nexora/
  docs/                      # design documents (first deliverable)
  contracts/                 # openapi.yaml, events/*.schema.json, python/ Protocol stubs (Integrator-owned, §14)
  coordination/              # OWNERSHIP.md (ownership globs), handoffs/<wave>/<agent>.md
  backend/
    nexora/                  # settings (base/dev/test/prod), asgi, celery, root urls
    apps/
      core/                  # tenancy (constants, context, TenantModel, EnableTenantRLS), audit, crypto, permissions, jobs/SSE
      accounts/  orgs/       # users, orgs, memberships, invitations, roles, permissions
      governance/            # dataset ACLs, column/row policies, user attributes, retention, access history, usage
      ai/                    # GeminiService, prompt registry, quota/circuit breaker, AIDataPolicy gate, embeddings
      connectors/            # data sources, credential vault, connector plugins
      datasets/  pipelines/  quality/  lineage/  warehouse/   # warehouse = nexora_wh loader + per-org roles
      semantic/              # semantic layer: tables, columns, metrics, schema cards
      sqlguard/              # sqlglot guard, secure-source rewrite, plan check, read-only executor
      nl2sql/                # question router, exact/semantic caches, deterministic builder, prompt assembly
      bi/                    # conversations, messages, query audit, saved queries, dashboards, exports, SSE endpoints
      customers/  products/  documents/  ml/  automation/  platform_admin/
    tests/                   # cross-cutting suites only: isolation/, security/, ai_eval/, integration/
  frontend/                  # Next.js app, design system in src/ui
  e2e/  perf/                # Playwright and k6 suites
  infra/                     # compose files, Caddy, Postgres init (roles, RLS), Grafana
  .github/workflows/
```

**Change from the first draft:** the old `bi/` app (and `core/ai`) is split into `ai`, `semantic`, `sqlguard`, `nl2sql` and `bi`. The original mixed four concerns that different agents own (§14). One owner per Django app means migrations and `models.py` never conflict. It is still one monolith; only the module boundaries changed.

---

## 4. Multi-tenancy design (recommendation)

**Hybrid model:**
- **Platform metadata:** one shared database `nexora_app`, with an `org_id` column on every tenant-owned table. Enforced in two layers:
  - App layer: a `TenantScopedManager` plus request middleware that resolves the org from the URL `/api/v1/orgs/{org_slug}/…` and the user's membership.
  - Database layer: **Postgres RLS** on every tenant table (see the canonical policy below).
  - The app connects as `nexora_app_rw`, which is **not** the table owner. `FORCE ROW LEVEL SECURITY` is on. Migrations run as a separate `nexora_migrator` role.
- **Tenant context has one name everywhere: `app.org_id`.**
  - It is defined once as `TENANT_GUC = "app.org_id"` in `backend/apps/core/tenancy/constants.py`.
  - Every layer reads it from there and never spells the string itself:
    - the request path: the DRF base view, with the middleware that clears the contextvar;
    - `tenant_context()`;
    - Celery's `TenantTask`;
    - the `EnableTenantRLS` migration operation;
    - raw SQL helpers;
    - tests.
  - A CI grep check fails if any other `app.*` tenant setting name appears in code, migrations or SQL files.
  - **Canonical RLS policy**, emitted by `EnableTenantRLS` for every tenant table:
    ```sql
    ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
    ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON <t>
      USING      (org_id = NULLIF(current_setting('app.org_id', true), '')::uuid)
      WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::uuid);
    ```
  - **Setting the context:** `tenant_context(org_id)` = `transaction.atomic()` + `SELECT set_config('app.org_id', %s, true)`. The `true` makes it transaction-local, equivalent to `SET LOCAL`, and it is never set at session level. So pooling, `CONN_MAX_AGE` and PgBouncer transaction mode cannot leak it.
  - **Request flow:** the DRF base view's `initial()` resolves the org from the URL org slug, checks for an active membership, sets the `ContextVar`, and opens `tenant_context`. Middleware only clears the `ContextVar` in a `finally` block. It does not set the database value, because DRF authentication has not run yet at middleware time.
  - `NULLIF` means a missing context yields **zero rows** instead of a cast error.
  - `TenantManager` **fails closed**: it raises an error if no org is set. `unscoped` access is allowed only in platform-ops code, enforced by a ruff banned-API rule.
  - Every `UniqueConstraint` includes `org`. Redis keys are prefixed `org:{id}:` and object keys `orgs/{id}/`.
- **Celery:** tasks take `org_id`/`actor_id` kwargs. `TenantTask` wraps execution in `tenant_context`, and a publish hook stamps and verifies an `org_id` header. Beat runs one dispatcher on a separate BYPASSRLS `ops` DB alias, which enqueues per-org tasks.
- **SSE:** no DB transaction is held open while streaming. Workers `XADD` events to a Redis Stream `org:{org}:job:{id}`. The async SSE view checks job ownership once, then `XREAD`s, which supports resuming via `Last-Event-ID`. Heartbeat every 15 s, `X-Accel-Buffering: no`.
- **Ingested analytical data:** a separate database `nexora_wh` with **one schema per org** (`org_<short_id>`), each with its own NOLOGIN reader role. NL2SQL against ingested data runs as that reader role inside a read-only transaction. The database itself then guarantees one org can never read another org's tables, even if the SQL guard had a bug.
- **Why not schema-per-tenant or database-per-tenant for metadata?** For a solo-built MVP they multiply migration effort, connection pools and ops work.
  - Shared + RLS is the standard SaaS starting point.
  - Its limits: noisy neighbours, no per-tenant backup or restore, and a single RLS mistake affects everyone. Tenant-isolation tests in CI mitigate the last one.
  - A dedicated-database tier for large customers can be added later, because org routing sits behind a database router.
- Every org gets its own users (via memberships), connections, datasets, AI configuration (model tier, budgets, data policy), dashboards and audit log, all keyed by `org_id`.

## 5. Guarded NL→SQL pipeline (Phase 4 core, a security-critical module)

1. The user selects a dataset. The server checks dataset permission (RBAC plus the dataset ACL).
2. **Schema context** is built from the *semantic layer*, not raw introspection.
   - The semantic layer holds only allowlisted tables and columns with business descriptions, sample-free stats, and KPI and metric definitions.
   - It is filtered by the user's column permissions, then compressed.
3. **Cache and deterministic path first:** normalized question plus schema hash goes to `bi_query_cache`. Saved KPI templates resolve without any Gemini call.
4. Gemini (REASONING model) returns **structured JSON**: `{intent, tables, columns, sql, assumptions, chart_hint}`.
5. **sql_guard** (sqlglot, Postgres dialect). Any failure here **rejects before execution** and the rejection is audited.
   - Exactly one statement, which must be `SELECT` or `WITH…SELECT`. No DML, DDL, `INTO`, locking clauses or comments.
   - **Function allowlist**, not a blocklist. Anything else is rejected, for example `pg_sleep`, `set_config`, `dblink`, `pg_read_file` and `lo_*`.
   - Every table reference is resolved against the allowlist, including those in CTEs and subqueries. `*` is expanded only to permitted columns. Any disallowed column is rejected.
   - Exactly one statement, which must be `SELECT`/`WITH…SELECT`/set operations. Reject `exp.Command`, `INTO`, `FOR UPDATE`, `WITH RECURSIVE`, system columns, `reg*` casts, and table functions in `FROM`. Cap input at 10 KB and 1,500 AST nodes.
   - Columns are resolved with `qualify(…, expand_stars=True, validate_qualify_columns=True)` against a schema that contains **only permitted columns**.
   - **Secure-source substitution:** in every scope (CTEs, subqueries, set-op branches, lateral), each base table is replaced with `(SELECT <cols or mask exprs> FROM org_x.ds_N_vM WHERE <row-policy predicates>) AS alias`. Predicates are built from the user's attributes with sqlglot builders and bound parameters, never string concatenation.
     - Masking happens at the source, so `WHERE`, `ORDER BY` and `row_to_json(t)` all see masked values.
     - An outer `OR 1=1` cannot reach the inner predicate.
   - The query is wrapped as `SELECT * FROM (<q>) _q LIMIT <org cap, default 1000>`, regenerated with comments stripped, and round-trip re-parsed.
   - **Plan check:** `EXPLAIN (FORMAT JSON, VERBOSE)` runs in the same transaction. Every relation must be in the allowed set and the cost must be below `NL2SQL_MAX_COST`. This catches cases where sqlglot and Postgres disagree.
6. **Execution** uses psycopg3 directly, not the ORM:
   - **Ingested data:** login role `wh_q_<org>` (USAGE on its own schema only; `default_transaction_read_only=on`, `statement_timeout`, `temp_file_limit`, `CONNECTION LIMIT 4`; `CREATE`/`TEMP` revoked).
   - **External Postgres:** the customer runs our setup script for a `nexora_ro` user. On save, Nexora checks `pg_roles` and `has_table_privilege` and refuses superuser, BYPASSRLS or any role with write privileges.
     - `sslmode=verify-full`.
     - **SSRF guard:** resolve the host once, reject private, loopback and link-local addresses, then connect with `hostaddr`.
     - Runs on the egress-isolated `query_ext` queue.
   - **Per query:** `BEGIN READ ONLY`, `SET LOCAL statement_timeout='15s'`, `lock_timeout`, `work_mem`, `search_path`. A named cursor with `fetchmany` stops at the row and byte caps.
   - Runs on Celery `query_wh` / `query_ext` queues, with SSE progress events.
7. Results go to the client as a table plus an auto-chosen chart spec.
   - What Gemini may see for the explanation depends on the org's **AI data policy** (§6).
   - Under the default `synthetic_only` policy, NL→SQL runs only on datasets classified `synthetic` or `public`.
   - Under `schema_only`, which requires admin consent and is the only free-tier option for real datasets, **no result values are sent**. The explanation is generated locally from templates using the computed insights.
8. Everything is written to `bi_query_audit`: question, generated SQL, rewritten SQL, params, guard verdict and reject code, policy fingerprint, executing role, plan cost, duration, rows, bytes, source (llm, cache or builder) and token usage.
   - The table is append-only, partitioned monthly and hash-chained.
   - The UI always shows the executed SQL (source and query transparency).
9. **Bypass corpus** in CI, which must be 100% rejected or neutralized:
   - stacked statements, data-modifying CTEs, `SELECT INTO`;
   - `pg_sleep`, `query_to_xml`, `dblink`, `set_config`, `current_setting`;
   - cross-schema names: quoted, Unicode-escaped, three-part;
   - `information_schema` and `pg_*` catalogs;
   - `t.*`, `row_to_json(t)`, `SELECT t FROM ds t`, `NATURAL JOIN`, denied columns in `WHERE`/`ORDER BY`;
   - `OR 1=1`, `UNION` around row policies, CTE shadowing;
   - nested comments, dollar quoting, recursive CTEs, cross-join blowups;
   - prompt injection hidden in data cell values.
   - Plus Hypothesis fuzzing.

Follow-up questions reuse conversation memory: the last N turns are summarized locally. Long histories get a Gemini summary only if the quota allows.

## 6. Gemini integration (`apps/ai`)

- **`GeminiService`** is the only place in the codebase that imports `google.genai`. A CI lint rule enforces this.
  - It provides `generate`, `generate_structured(schema)`, `stream` (SSE passthrough) and `embed` (optional mode).
- **Prompt registry:** versioned prompt templates in the database (`ai_prompt_template`) with code defaults, so each response can be traced to the prompt version that produced it.
- **Logging:** `ai_request_log` records org, user, feature, model, prompt version, token counts from `usage_metadata`, latency, status and a hash of the prompt. Full text is logged only if the org opts in.
- **Rate limiting and quota:**
  - Redis token buckets per project (global RPM/RPD mirroring the AI Studio limits, set by configuration), per org (daily budget) and per user.
  - Retries use exponential backoff with jitter on 429/503 and honour any retry delay the API sends. No retry once the daily quota is gone.
  - A circuit breaker opens until midnight Pacific when RPD is exhausted. The UI then shows "AI quota reached — resets at HH:MM (your timezone)".
  - **There is no fallback to another provider.** Non-AI features keep working: manual SQL for permitted roles, saved KPIs, dashboards.
- **Minimizing calls.** Questions are resolved in this order, with the LLM last:
  1. **Exact cache:** org + dataset version + normalized question.
  2. **Semantic cache:** local embeddings in pgvector with cosine ≥ 0.93; date and number slots are re-bound into a stored SQL template. The cache stores SQL from *before* policy rewriting and the guard re-runs for every user, so reuse is safe within an org and never crosses orgs.
  3. **Deterministic builder:** semantic-layer metrics and dimensions, rapidfuzz slot filling, SQL built with sqlglot. The visual query builder uses the same path.
  4. **One Gemini call** returning SQL, explanation and chart, with at most one repair call. FAST model by default, REASONING only for multi-table questions or repairs.
- **Other ways to save calls:**
  - Schema cards are capped at about 1.5k tokens, using top-k columns.
  - Insights (top-N, period-over-period, z-score anomalies, trend) are computed locally with Polars and narrated from templates by default. Gemini narration is optional and happens only when the org's `AIDataPolicy` allows values for that asset.
  - Profiling, quality checks, EDA statistics and dedup are all local.
- **Never** spread load across multiple Google projects or keys to get around limits; that breaches Google's terms.
- **Production route, still Gemini only:** a paid Gemini API key, or Gemini on **Vertex AI** through the same `google-genai` SDK (`vertexai=True`) for enterprise data terms. `GEMINI_TIER=free|paid|vertex`. With `free`, the server refuses any `AIDataPolicy` other than `synthetic_only` or `schema_only`.
- **AI data policy:** one enum, `AIDataPolicy`, used in the DB column `organization.ai_data_policy`, the API, the UI, the docs and the gate code. Each column below is a separate rule:
  - **Free tier:** whether the value may be selected when `GEMINI_TIER=free`.
  - **Schema to Gemini:** table and column names, types, business descriptions, and metric definitions.
  - **Values to Gemini:** data values, query results, or document text.

  | Value | Free tier | Schema to Gemini | Values to Gemini |
  |---|---|---|---|
  | `synthetic_only` **(default)** | ✅ allowed | Only for assets classified `synthetic` or `public` | **Only for datasets and documents classified `synthetic` or `public`.** An admin attests to the classification, and the attestation is audited. Results, small samples and document chunks of those assets may be sent. **Real (unclassified) assets get no Gemini calls at all, not even schema.** |
  | `schema_only` | ✅ allowed, **only after explicit org-admin consent** | yes, for real assets; PII-tagged columns are aliased, and admins can exclude whole tables or columns from prompts | **None** for real assets. No rows, results or document text. Explanations and insights come from local templates. Synthetic and public assets keep `synthetic_only` behaviour. |
  | `aggregates_only` | ❌ refused | yes | Aggregated results only, with a minimum group size of k ≥ 5 and PII columns excluded. No row-level records. |
  | `full` | ❌ refused | yes | Redacted row samples and document chunks. Requires a paid key or Vertex, plus a recorded admin consent. |

  - **Default everywhere is `synthetic_only`:**
    - the model field default and the DB column default (`DEFAULT 'synthetic_only'`);
    - the org-creation service (sign-up, super-admin create, seed or fixtures);
    - API serializers, which treat the field as read-only on create;
    - the frontend's initial state.
    - A CI test asserts that a newly created org has `synthetic_only` through every creation path.
  - **Why `schema_only` needs consent:** table and column names, descriptions and metric definitions can themselves be confidential. To move to `schema_only`:
    - an **org Owner/Admin** must give explicit consent in a dedicated dialog that states what will be sent to Google and the free-tier data-use terms, with re-authentication;
    - the consent is recorded in `ai_policy_consent` (org, policy, admin user, consent-text version, Gemini tier, timestamp, IP) and in `audit_event`;
    - consent can be revoked at any time, which reverts the org to `synthetic_only` immediately;
    - a policy change by anyone without the Owner/Admin role is rejected;
    - a consent-text version bump requires re-consent.
  - **Enforced in three places:**
    1. API validation refuses `aggregates_only` and `full` when `GEMINI_TIER=free`, and refuses `schema_only` without a valid consent record.
    2. A DB check is enforced at call time inside `GeminiService`. Every prompt builder declares a `PayloadClass` (`SCHEMA`, `VALUES`, `DOCUMENT_TEXT`), and the gate rejects any payload class the policy does not allow before the request is sent.
    3. PII redaction (regex plus PII column tags) runs on all prompts as a last line of defense.
  - **What works on the free tier:**
    - **With `synthetic_only` (default):** every AI feature, but only on assets classified `synthetic` or `public`. On real assets, only the non-AI features work: ingestion, profiling, quality checks, the manual or visual query builder, dashboards, and local OCR and semantic search.
    - **With `schema_only` (consented):** additionally, NL→SQL and local insights on real datasets. Generative document Q&A and Gemini-written narration stay disabled for real assets.
  - **No real customer records or confidential data ever reach the free tier.**
- Keys come only from the environment or secret store (`GEMINI_API_KEY`). There is one platform key for the MVP. A per-org bring-your-own Gemini key, encrypted, is planned for Phase 5 or 13.

## 7. Database schema (core entities; full DDL goes in `DATABASE_SCHEMA.md` and `DATABASE_ERD.md`)

All tenant tables share these columns: `id uuid`, `org_id uuid`, `created_at`, `updated_at`, `created_by`. RLS is enabled on all of them.

- **Identity and tenancy:** `organization` (plan, status, region, `ai_data_policy` AIDataPolicy, `NOT NULL DEFAULT 'synthetic_only'`), `ai_policy_consent` (org, policy, consenting admin, consent-text version, Gemini tier, ip, created_at, revoked_at; append-only), `user`, `membership` (user, org, role), `invitation` (token hash, expiry), `role` (system and custom), `permission`, `role_permission`, `api_key` (hash, scopes), `mfa_device`, `session`.
- **Audit and governance:** `audit_event` (append-only, actor, action, target, ip, diff, request_id; Postgres trigger blocks UPDATE and DELETE), `access_history`, `dataset_acl` (principal → dataset → read/query/admin), `column_policy` (allow/deny/mask), `row_policy` (predicate template, bound attributes), `user_attribute`, `retention_policy`, `pii_tag`.
- **Data engineering:**
  - `data_source` (type, config, credential_id), `credential` (envelope-encrypted with AES-GCM; data key wrapped by a master key from env or KMS).
  - `dataset`, `dataset_version`, `dataset_column` (detected type, semantic type, PII flag, stats JSON), `upload` (object key, checksum, size).
  - `pipeline`, `pipeline_version` (DAG JSON: source → steps → destination), `pipeline_step`, `pipeline_schedule`, `pipeline_run` (status, started, finished, duration, rows in/out, retry_count, error), `pipeline_run_log`.
  - `quality_rule`, `quality_result`, `quality_report`, `lineage_edge` (node type and id → node type and id, run_id).
- **Conversational BI:** `semantic_model`, `semantic_table`, `semantic_column`, `metric_definition`, `conversation`, `message`, `bi_query_audit`, `bi_query_cache`, `saved_query`, `dashboard`, `dashboard_widget`, `report_export`.
- **AI:** `ai_prompt_template`, `ai_request_log`, `ai_usage_daily` (org, model, requests, tokens), `ai_org_config`.
- **Phase 6+:**
  - `customer_source_record`, `customer_profile`, `identity_match` (score, status, reviewer), `customer_event`, `customer_segment`, `customer_score` (churn, CLV, confidence, explanation).
  - `product_source_record`, `product_master`, `product_attribute`, `product_match`, `product_correction` (proposed, approved, rejected), `product_quality_score`.
  - `document`, `document_page`, `document_chunk` (text, page, section, `embedding vector(N)` with HNSW index), `document_entity`, `document_table`, `extraction_job`.
  - `ml_dataset_snapshot`, `ml_experiment`, `ml_model`, `ml_model_version` (metrics JSON, artifact key, feature schema), `ml_prediction_log`, `ml_drift_report`.
  - `workflow`, `workflow_version`, `workflow_trigger`, `workflow_run`, `workflow_step_run`, `approval_request`, `agent_config` (allowed tools and scopes).
- **PII detection** at ingestion: column-name heuristics, regex and Microsoft Presidio (open-source). Hindi and Gujarati PII coverage is limited, which is a documented gap. PII columns never enter prompts.
- **Embedding dimension** is fixed per deployment by the chosen model and recorded on each document collection. Changing the model triggers a re-embedding job.
  - Default: `intfloat/multilingual-e5-base` (768 dimensions, about 1.1 GB, CPU-friendly; covers hi and gu).
  - Evaluate against `BAAI/bge-m3` (1024 dimensions, better quality, about 2.3 GB RAM) on a small English/Hindi/Gujarati retrieval test set in Phase 8.

## 8. API design (full spec goes in `API_SPECIFICATION.md`, generated as OpenAPI by drf-spectacular)

- REST under `/api/v1`. Tenant-scoped resources sit under `/api/v1/orgs/{org}/…`. Platform routes sit under `/api/v1/platform/…` (super-admin only).
- Conventions: cursor pagination, `Idempotency-Key` on POSTs that start jobs, RFC 7807 error bodies, request IDs, and per-key and per-user rate limits.
- Long-running work returns `202 {job_id}`. Progress arrives via SSE `GET …/jobs/{id}/events` or polling.
- AI chat streams over SSE: `POST …/bi/conversations/{id}/messages` returns `text/event-stream` with `status|sql|result|token|done|error` events.
- Representative groups: `auth/*`, `orgs`, `members`, `invitations`, `roles`, `data-sources` (+ `/test`, `/introspect`), `uploads`, `datasets` (+ `/profile`, `/preview`, `/quality`), `pipelines` (+ `/runs`, `/runs/{id}/retry`), `lineage`, `bi/semantic-models`, `bi/conversations`, `bi/queries`, `dashboards`, `governance/policies`, `audit-events`, `usage`, and later `customers`, `products`, `documents`, `ml/*`, `workflows`, `approvals`.
- **Connector plugin interface** (`BaseConnector`: `test`, `introspect`, `read_batches`, `incremental_cursor`) lets MySQL, REST, Snowflake and others be added without touching the pipeline engine.

---

## 9. Phased roadmap (solo developer, realistic)

**The MVP is Phases 1–5 plus the basic slices of Phases 11 and 12, about 15 weeks.** Phases are delivered as vertical slices. A phase is "done" only when its acceptance tests pass in CI, not when its UI exists.

| # | Phase | Estimate | Depends on | Notes |
|---|---|---|---|---|
| 1 | Architecture, docs, foundation | 2 wk | — | Docs (week 1, then pause for review). Then monorepo, Compose stack, Django and Next.js skeletons, CI, design tokens, GeminiService stub with live free-tier verification, `.env.example`. |
| 2 | Auth, orgs, RBAC | 2 wk | 1 | Sign-up, login, sessions, invitations (Mailpit in dev), roles (Owner, Admin, Analyst, Viewer, plus custom), RLS, audit log, tenant-isolation test suite. |
| 3 | Data Engineering Hub | 4 wk | 2 | CSV, Excel, JSON and PostgreSQL. Schema detection, profiling, validation, cleaning, dedup, transforms. Linear-step pipeline builder (React Flow canvas). Scheduling, retries, run history, failure alerts, quality reports, basic lineage. MySQL and REST connectors only if time remains. |
| 4 | Conversational BI | 3 wk | 3 (+2) | Semantic layer, guarded NL2SQL, SSE chat, charts and tables, follow-ups, query history, CSV/XLSX/PDF export, transparency panel, quota UX. |
| 5 | Governance | 2 wk | 2–4 | Column and row policies (UI plus guard integration), dataset ACLs, access history, retention jobs, API and AI usage monitoring, PII tagging. |
| 11a | Org-admin and BI dashboards (basic) | inside 2–5 | | Built incrementally alongside the features they show. |
| 12a | Security testing baseline | inside 2–5 | | SQL-guard fuzzing, isolation tests and dependency scanning run in CI from Phase 2. |
| — | **MVP total plus buffer** | **~15 wk** | | About 3.5 months. |
| 8 | Document Intelligence | 3 wk | 1–2 | Independent of 6/7/9. Extraction, OCR (en/hi/gu), chunking, local embeddings, pgvector RAG with page citations, summarization, Q&A, classification, entity and table extraction. |
| 9 | ML Workspace | 3–4 wk | 3 | EDA, preprocessing, model recommendation, training (sklearn and XGBoost), metrics, comparison, versioning, Prediction API, drift monitoring. |
| 13 | Deployment | 1 wk | MVP | Single-VM Compose deployment with TLS, backups, monitoring. |
| — | **Within 6 months** | **~22–23 wk** | | MVP + 8 + 9 (at **prototype** status) + 13. |
| 6 | Customer 360 | 3–4 wk | 3, 9 | Identity resolution with human review queue, segmentation, churn, CLV, next best action. |
| 7 | Product 360 | 3 wk | 3, (8) | SKU normalization, dedup, attribute extraction, reviewable corrections, semantic search. |
| 10 | Automation and agents | 4 wk | 2–5, (8) | Workflow builder, triggers, approvals, tool-scoped agents. |
| 11b, 12b | Super-admin platform monitoring, full hardening | 2–3 wk | all | |
| — | **Full scope** | **~40–45 wk solo** | | **Phases 6, 7 and 10 fall beyond the 6-month window and are marked Planned.** |

- The estimates above are **sequential**. §15 gives the parallel-agent wave plan, with a realistic MVP target of about 12 weeks.
- **Recommended order after the MVP:** 8 → 9 → 13 → 6 → 7 → 10. Churn and CLV in Phase 6 reuse Phase 9's training and registry service, so building 9 first avoids duplicate work.
- **Independent once the MVP exists:** Phases 8, 9 and 13 can be built in any order. Phase 7 is independent of 6. Phase 10 depends on the others' APIs.

Each phase's full breakdown goes in `IMPLEMENTATION_ROADMAP.md`: objectives, requirements, dependencies, migrations, endpoints, frontend components, backend services, tests, acceptance criteria and risks. Representative acceptance criteria:
- **P2:** an automated suite proves that a user in org A gets 404 on every org-B resource across all endpoints, and that direct SQL as `nexora_app_rw` without a context returns 0 rows.
- **P3:** a 1M-row CSV ingests in under 2 minutes on the dev box, with profile and quality report produced; a failed pipeline retries N times and records the error; lineage shows source → dataset.
- **P4:** a 60-case adversarial corpus (DML, stacked queries, function abuse, cross-schema references, comment tricks, CTE smuggling, `RESET ROLE`, `set_config`, `pg_sleep`, Unicode tricks) is 100% rejected. A golden set of 40 NL questions over a synthetic sales dataset reaches 80% or more execution-correct, using recorded Gemini responses so CI uses no quota.
- **P5:** row and column policies change query results for the restricted user exactly as specified; every query appears in the audit log.
  - New orgs default to `synthetic_only` on every creation path.
  - `schema_only` is refused without a valid Owner/Admin consent record, and revoking consent reverts the org immediately.
  - With the default policy, no Gemini request carries a real asset's schema or values. This is proven by asserting on the payload-class gate.

## 10. Status labelling (enforced in docs and UI)

Every module carries a status:
- **Working:** implemented, with tests passing in CI.
- **Prototype:** implemented, but with limited tests or scale.
- **Mock:** UI or demo data only.
- **Planned:** designed, not built.

A `FEATURE_STATUS.md` matrix is kept up to date, and the UI shows a badge on Prototype and Mock modules. No feature is called production-ready without its acceptance tests.

## 11. Things that need paid services (flagged, not assumed)

- **Gemini paid tier:** needed for real enterprise or personal data, any EEA, UK or Swiss users, and production-level quotas. Still Gemini, as the terms allow.
- Cloud VM, managed Postgres and object storage for production. MinIO and a single VM keep this low-cost.
- Transactional email provider for production invitations and alerts. Mailpit is used in dev.
- KMS or secret manager (optional; environment master key for the MVP).
- GitHub Actions minutes, which are limited for private repos. Domain name (TLS via Let's Encrypt is free).
- **Nothing in the MVP requires a paid AI provider.** All ML, OCR, embeddings, profiling and dedup run locally on open-source software.

## 12. Key technical risks (expanded in each document)

1. **Gemini free-tier quota is very small and changes without notice.** Mitigation: caching, deterministic paths, budgets, a visible quota UX, and the assumption that only a demo is possible without a paid key.
2. **Free-tier data-use terms.** Mitigation: free tier limited to `synthetic_only` and `schema_only`, the payload-class gate, and PII redaction.
3. **NL2SQL safety.** Mitigation: defense in depth (guard → AST rewrite → database role → read-only transaction → timeout → audit) and adversarial tests.
4. **NL2SQL accuracy.** Mitigation: semantic layer, golden evaluation set, users can see and correct the SQL.
5. **RLS mistakes or forgotten `org_id`.** Mitigation: base model plus lint, isolation suite, RLS on everything.
6. **Solo scope versus 13 phases.** Mitigation: strict MVP, status labels, vertical slices.
7. **Windows development and Celery.** Mitigation: Compose for everything server-side.
8. **CPU memory for embeddings, OCR and ML on the dev box.** Mitigation: a dedicated ml/ai worker with a concurrency of 1, and model sizes chosen for CPU.
9. **Large file ingestion.** Mitigation: streaming and chunked Polars reads, size limits, object storage.
10. **Credential handling, SSRF and connectors.** Mitigation: envelope encryption, a read-only privilege check, IP pinning, egress-isolated workers, and secrets never returned or logged.
12. **Hostile uploads.** Mitigation:
    - size caps and zip-bomb ratio checks on xlsx/docx;
    - `defusedxml`, `openpyxl` read-only mode;
    - Polars streaming and worker memory limits;
    - escaping CSV/Excel formula injection on export.
13. **The project path contains spaces** (`D:\nexora enterprice ai`). Mitigation: mount it as `/app` in containers and quote paths in scripts.
11. **Model deprecations.** Mitigation: model IDs in configuration plus the startup validation check.

---

## 13. First execution step after approval: write the 14 documents

Create `D:\nexora enterprice ai\docs\` with these files. Each is detailed and implementation-ready, expanding the sections above:

1. `PROJECT_VISION.md`: problem, personas, MVP definition, non-goals, success metrics, status legend.
2. `SYSTEM_ARCHITECTURE.md`: modular monolith, process topology, app boundaries, tech-choice rationale, Windows and Compose notes.
3. `DATABASE_SCHEMA.md`: every table with columns, types, constraints, indexes, RLS policy, and owning phase.
4. `DATABASE_ERD.md`: Mermaid ER diagrams per domain.
5. `API_SPECIFICATION.md`: conventions, auth, errors, pagination, SSE event contract, endpoint tables per phase.
6. `DATA_ENGINEERING_DESIGN.md`: connector SDK, ingestion, profiling, quality rules, pipeline DAG model, scheduler, retries, lineage.
7. `AI_ARCHITECTURE.md`: GeminiService, verified model and quota table (dated, with re-verify steps), prompt registry, quota and circuit breaker, data policy, NL2SQL, RAG, agent guardrails.
8. `ML_ARCHITECTURE.md`: workspace flow, model-recommendation rules, metrics, registry, prediction API, monitoring.
9. `SECURITY_ARCHITECTURE.md`: threat model, authn/authz, SQL guard, secrets, encryption, audit, OWASP mapping.
10. `MULTI_TENANCY_DESIGN.md`: comparison of tenancy options, the hybrid recommendation, RLS mechanics, its limits, migration path.
11. `UI_UX_SPECIFICATION.md`: design tokens, typography, layout shell, navigation map, key screens, dashboards A–D, light and dark themes, responsiveness.
12. `TESTING_STRATEGY.md`: test pyramid, isolation and SQL-security suites, AI output validation with recorded responses, ML evaluation tests, performance targets.
13. `DEPLOYMENT_ARCHITECTURE.md`: Compose for dev and prod, CI/CD, backups, observability, scaling path.
14. `IMPLEMENTATION_ROADMAP.md`: all 13 phases with the full per-phase breakdown, estimates (sequential and parallel), dependencies, the independence map and the wave plan.

Also:
- `docs/AGENT_DEVELOPMENT_STRATEGY.md`: §14 in full, with roles, ownership, contracts, wave plan and merge protocol.
- `coordination/OWNERSHIP.md`: the machine-readable glob → agent table that the ownership check reads.
- `docs/FEATURE_STATUS.md`: everything starts as Planned.
- `docs/README.md`: an index.

The Integrator writes the docs sequentially, because the docs *are* the contracts every agent will build against; parallel writers would risk inconsistencies.

**Then stop and ask for review. No application code, scaffolding or git init until the docs are approved.**

---

## 14. Parallel agent development strategy (Claude Desktop Dynamic Workflows)

The strategy is contract-first, ownership-based and scheduled in waves. Each wave is one Dynamic Workflow run with at most 5 concurrent agents, which stays inside the medium workflow-size guideline and the laptop's RAM. **The user approves at the end of every wave.**

### 14.1 Rules that prevent concurrent edits to shared files
1. **One owner per file per wave.** Ownership is declared as globs in `coordination/OWNERSHIP.md`.
   - The ownership unit is the Django app: an app's models, migrations, services, views, URLs and unit tests all belong to one agent. This is why the old `bi/` was split (§3).
2. **Each agent works in its own git worktree** on branch `agent/<role>/w<N>`. Only the Integrator merges.
3. **Ownership check in CI:** `scripts/check_ownership.py` diffs the agent's branch against the wave base and fails on any path outside that agent's globs.
4. **Shared files are Integrator-only:** `backend/nexora/**` (settings, root `urls.py`, `asgi.py`, `celery.py`), `backend/pyproject.toml` and its lockfile, `backend/conftest.py`, `docker-compose*.yml`, `infra/**`, `.github/**`, `.env.example`, `contracts/**`, `docs/**`, `scripts/**`, root dotfiles.
5. **Pre-wiring in the Phase 1 scaffold** keeps shared files from changing mid-wave:
   - all planned Django apps are created empty and registered in `INSTALLED_APPS`;
   - root URLs include every app's `urls.py`;
   - all Celery queues and routes are declared;
   - all env vars are listed in `.env.example`;
   - each wave's Python dependencies are installed by the Integrator before the wave starts.
6. **Change requests instead of edits.** An agent that needs a shared-file change (a dependency, a setting, an env var, a contract change, or an FK into another agent's app) writes it under "Requests" in its own handoff file `coordination/handoffs/w<N>/<agent>.md`. Because it is a per-agent file, there is no contention. The Integrator applies requests at a mid-wave checkpoint or between waves.
7. **Cross-app imports only through contracts:** `apps/<x>/services.py`, which implements the Protocols in `contracts/python/`. import-linter enforces the layering: `core` ← `orgs`/`governance` ← data apps ← `nl2sql`/`bi`. `ai` imports only `core`/`orgs`.
8. **Isolated test resources:** one shared infra stack (Postgres+pgvector, Redis, MinIO), run by the Integrator. Each agent gets its own test DB name (`test_nexora_<agent>`), Redis DB index and MinIO bucket prefix, so parallel test runs never collide.
9. **Gemini quota is shared per Google project, so live calls are serialized.**
   - Agents use `FakeGeminiService` and recorded cassettes.
   - Only the AI/ML agent records cassettes, within a dev budget enforced by the limiter (`DEV_AGENT_RPD`).
   - Only the Integrator runs live smoke tests.

### 14.2 Roles, ownership and interfaces
| Agent | Owns (write access) | Provides (contracts) | Consumes |
|---|---|---|---|
| **Main Architect / Integrator** (main session, user-gated) | Shared files (§14.1 rule 4), `coordination/**`, `docs/FEATURE_STATUS.md` | Contracts written **before** each wave; wave launch; merges; full CI; live Gemini smoke tests; status updates (only on green acceptance tests) | All handoff files |
| **Backend** | `backend/apps/{core,accounts,orgs,governance,bi,platform_admin,automation}/**` | Tenancy (`TENANT_GUC`, `tenant_context`, `TenantModel`, `TenantTask`, `EnableTenantRLS`); auth/RBAC APIs; `core.audit.record()`; `core.jobs.publish()` + SSE; `governance.services.effective_policy(user, dataset) → QueryPolicy`; `bi` orchestration endpoints | `sqlguard`, `nl2sql`, `ai.policy` |
| **Data Engineering** | `backend/apps/{connectors,datasets,pipelines,quality,lineage,warehouse,semantic,sqlguard,customers,products}/**` | Ingestion, profiling and pipeline APIs; `warehouse.provision(org)`; `semantic.schema_card(ds_version, allowed_cols)`; `sqlguard.validate(sql, QueryPolicy) → GuardResult`; `sqlguard.execute(GuardResult, target) → ResultSet` | Tenancy, `effective_policy`, jobs |
| **AI/ML** | `backend/apps/{ai,nl2sql,documents,ml}/**` | `ai.GeminiService` (generate, structured, stream, embed); `ai.policy.check(org, PayloadClass, asset)`; local embedding service; `nl2sql.propose(question, ctx) → SqlProposal`; insights engine; later ML training, registry and prediction services | Tenancy, `semantic.schema_card`, `sqlguard.validate` (for the repair loop) |
| **Frontend** | `frontend/**`, including its own `package.json`/lockfile (no other agent touches the frontend) | All UI, the design system, and the typed client generated from `contracts/openapi.yaml` into `frontend/src/api/generated/` (never hand-edited) | Prism mock server (from contracts) until the backend lands, then the real API |
| **QA/Security** | `backend/tests/{isolation,security,ai_eval,integration}/**`, `backend/testing/**` (shared fakes and fixtures built from the Protocols), `testdata/**` (synthetic dataset generators), `e2e/**`, `perf/**` | Isolation suite, SQL attack corpus and Hypothesis fuzzing, golden NL2SQL set, upload fuzzing, Playwright E2E, k6 perf, independent security review of `sqlguard`, tenancy and `ai.policy` | Contracts only. It never edits app code; defects go into its handoff file for the owner to fix. |

### 14.3 Contracts (written by the Integrator, frozen during a wave)
- **REST:** `contracts/openapi.yaml`, versioned per wave.
  - CI generates the drf-spectacular schema and diffs it against the contract with oasdiff; drift fails the build.
  - The Frontend generates its client from the contract and develops against a Prism mock.
- **SSE:** `contracts/events/*.schema.json` covers job events and chat events (`status|sql|result|token|done|error`).
- **Python service contracts:** `contracts/python/{tenancy,policy,guard,ai,nl2sql,jobs}.py` hold Protocols and frozen dataclasses (`QueryPolicy`, `GuardResult`, `SqlProposal`, `ResultSet`, `JobEvent`, `PayloadClass`, `AIDataPolicy`).
- **Database:** each app's tables are defined in `DATABASE_SCHEMA.md`. Cross-app FKs point only at `orgs`, `accounts` and `datasets` models named in the contract.

### 14.4 Integration protocol (per wave)
1. The Integrator publishes the contracts and dependencies, then launches the wave workflow. Agents work in their worktrees and finish with their handoff file.
2. Merge order: **Backend → Data Engineering → AI/ML → Frontend → QA**. Full CI runs after each merge, including the ownership check, contract diff, isolation suite and SQL attack corpus.
3. Cross-agent flows are verified by QA-owned `backend/tests/integration/`. The Integrator fixes only glue in Integrator-owned files. Defects in agent-owned files go back to their owner in a short follow-up run.
4. The Integrator updates `FEATURE_STATUS.md` and presents the wave report to the user. **The next wave starts only after the user approves.**

### 14.5 Execution mechanism: verified availability and fallback

**Verified 2026-11-17 in this session:**
- **Available:** the Workflow tool (Dynamic Workflows) is exposed in this Claude Desktop Code session, so it is enabled for your account here. I cannot read your subscription plan directly; whether the tool is present in a session is the reliable signal. W0 re-checks it before W1.
- **Confirmed from its reference documentation:**
  - `agent(…, {isolation: 'worktree'})` runs each agent in its own fresh git worktree.
  - Concurrency is capped at min(16, CPUs − 2), and this session's size guideline is "medium" (under 10 agents).
  - Each run needs your explicit opt-in ("use a workflow").
  - Progress is visible in `/workflows`, and runs can be resumed.
- **Constraints:**
  1. It needs a git repository, and the folder is not one yet. W0 runs `git init` and an initial commit first.
  2. Workflow agents run headless, so you cannot steer them mid-run. They suit bounded tasks of hours, not multi-week streams.
  3. Unchanged worktrees are removed automatically. Where changed worktrees and branches end up must be confirmed with a **one-agent dry run in W0** before W1 relies on it.

**Mode A: Dynamic Workflows (primary, if the W0 dry run passes)**
- Each wave is split into 2–4 **task batches**, each about one day of agent work. Each batch is one workflow run with one agent per active role and `isolation: 'worktree'`.
- Each agent's prompt carries its ownership globs, the contract version and its batch tasks.
- Each agent returns a schema-validated handoff: files changed, tests run, requests, open issues.
- A final QA-agent stage in the same pipeline runs the ownership check and test suites on each branch.
- The Integrator merges in the §14.4 order, and you approve before the next batch.

**Mode B: parallel Claude Desktop Code sessions with isolated worktrees (fallback)**
- **When to use it:** if Workflow is unavailable or the dry run fails, or for long-running work you want to steer interactively.
- **Launching:** the Integrator (this main session) starts **one Desktop Code session per active role per wave** with `use_worktree: true` and fresh context. That gives each session its own worktree and branch, `agent/<role>/w<N>`.
- **Brief for each session:** its role; its ownership globs, quoted from `OWNERSHIP.md`; the contract version; its wave tasks; its handoff file path. It also states the rules: "edit only inside your globs; commit only on your branch; never merge, push or switch branches".
- **Concurrency:** at most 4–5 sessions at once. You can open, steer or stop each one from the sidebar or split view.
- **Reports:** sessions report back when they finish a turn. The Integrator **checks each branch's diff and test results, not just the report**, before merging. Merges happen in the §14.4 order and only with your approval.

**Common to both modes:** the same five agent roles plus the Integrator, ownership globs, contracts, waves, merge order, CI gates and testing architecture. **Only the launcher differs.**

## 15. MVP estimate review and safe parallelism

### 15.1 Wave plan
| Wave | Weeks | Backend | Data Engineering | AI/ML | Frontend | QA/Security |
|---|---|---|---|---|---|---|
| **W0** (Integrator only, serial) | 1–2 | — | — | — | — | — |
| **W1**: Phase 2 + foundations | 3–4 | **Tenancy primitives first** (days 1–3, merged at a mid-wave "tenancy checkpoint"), then auth, orgs, RBAC, invitations, audit | Pure Polars parsing, profiling and schema-detection engine; pure `sqlguard.validate` (sqlglot, no DB) | `GeminiService`, limiter, circuit breaker, prompt registry, `AIDataPolicy` gate, cassette recorder | Design system, app shell, auth and org screens (Prism mock) | Synthetic data generators, shared fakes, isolation-suite framework, SQL attack corpus (TDD against the guard Protocol) |
| **W2**: Phase 3 + Phase 4 prep | 5–8 | Governance models + `effective_policy`, jobs/SSE (Redis Streams), usage metering, access history | Connectors (CSV, Excel, JSON, PG), uploads, warehouse provisioning and loader, pipelines, scheduling, retries, quality, lineage, semantic layer; secure-source rewrite, executor, plan check | NL2SQL router, exact and semantic caches, deterministic builder, schema-card prompts, local embeddings, eval harness | Data sources, uploads, dataset explorer, pipeline canvas, run history, quality reports, policy screens | Isolation tests on new endpoints, upload fuzzing, guard fuzzing, golden NL question set |
| **W3**: Phase 4 integration + Phase 5 | 9–11 | `bi` chat SSE orchestration (`nl2sql` → `sqlguard` → executor → audit), dashboards, exports, retention jobs | 1M-row ingest performance, result caps, external PG hardening (SSRF, role check) | Insights engine, template narration, quota-state API, eval tuning to ≥ 80% | Chat UI, charts, transparency panel, BI and org-admin dashboards, quota banners | Playwright E2E, policy-result tests, k6 baseline, security review |
| **W4**: Hardening | 12 | Fixes | Fixes | Fixes | Fixes | Regression run |

- **W0 detail:** docs, then a pause for review, then `git init`, scaffold, pre-wiring, ownership check, CI, contracts v1, and the execution-mode check: a one-agent worktree dry run for Mode A, or one test session for Mode B (§14.5).
- **W4 detail:** the Integrator produces the release candidate and updates `FEATURE_STATUS`.

### 15.2 What cannot be parallelized (serial constraints)
- Scaffold, pre-wiring and contracts come before any agent starts.
- Tenancy primitives come before any tenant-owned model, hence the mid-W1 checkpoint.
- Warehouse provisioning comes before executor tests on real data.
- The end-to-end NL→SQL flow needs `governance`, `sqlguard`, `nl2sql` and `bi` together, so it lands in W3.
- Live Gemini runs (cassette recording, golden eval) are serialized because quota is per project.
- Any shared-file change is Integrator-only and happens between waves.

### 15.3 Estimate verdict
- **The 15-week sequential estimate holds.** With parallel agents the realistic target is **about 12 weeks**: W0 2 + W1 2 + W2 4 + W3 3 + W4 1.
- That is a saving of roughly 20%, **not** five times faster, because:
  - **The critical path stays serial:** scaffold (2) → tenancy (≈1) → datasets and warehouse (≈3) → guard, executor and policy (≈2) → BI end-to-end (≈2) → hardening (1). That is about 11 weeks no matter how many agents run.
  - **Review time is the real bottleneck.** You review and approve every wave merge; integration overhead is about 15–20%.
  - **The shared Gemini free-tier quota** serializes live AI testing.
  - **Laptop limits:** RAM and CPU for 4–5 agents running Docker-backed tests on Windows.
  - Contract changes cause rework.
- **Recommendation:** target 12 weeks, commit to 15. Use the buffer for hardening, not new scope.
- **After the MVP, the same model applies.** These can run in one wave:
  - P8 (AI/ML + Frontend + QA);
  - P13 (Integrator);
  - P9 groundwork (AI/ML, once P8 is merged).
- Then P6 and P7 can run in parallel, because `customers` and `products` are separate apps; give each one its own Data Engineering agent instance.
- P10 stays last. In the 6-month window, P8 and P9 can realistically reach **Working** if their acceptance tests pass, P6 and P7 **Prototype**, and P10 stays **Planned**.

## Verification

- **Docs step:** every one of the 14 files, plus the agent-strategy doc and `OWNERSHIP.md`, exists and covers all items the brief requires for its topic, including the per-phase fields.
  - Mermaid diagrams render in the file preview. Gemini facts carry a "verified on" date and source links.
  - **Consistency pass:** a grep across `docs/` shows only `app.org_id` as the tenant setting name, only the four `AIDataPolicy` values, `synthetic_only` as the only default, and the consent requirement for `schema_only`.
  - Every path in `OWNERSHIP.md` maps to exactly one agent, with no overlapping globs.
- **Every wave:** the ownership check, the OpenAPI contract diff and import-linter are green before merge.
- **Later phases:**
  - CI runs pytest (unit, API, isolation, SQL-guard adversarial corpus, recorded-response AI tests), Vitest and Playwright E2E against the Compose stack.
  - A manual smoke test starts the app with Compose, then: create org → upload the synthetic CSV → run the pipeline → ask an NL question → confirm the SQL, results, chart and audit entry appear, and that a second org cannot see any of it.
