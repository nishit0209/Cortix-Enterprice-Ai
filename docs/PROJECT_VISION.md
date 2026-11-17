# Nexora Enterprise AI: Project Vision

> Status: **Design approved for documentation. No code exists yet.** Last updated 2026-11-17.
> Every feature named here is **Planned** until `FEATURE_STATUS.md` says otherwise.

## 1. What Nexora is

Nexora is a multi-tenant **Enterprise AI and Data Intelligence Platform**. An organization can:
1. connect fragmented data sources (files, databases, APIs);
2. ingest, validate, clean, deduplicate, transform and govern that data;
3. ask business questions in natural language and get **auditable SQL, tables, charts and explanations**;
4. then expand into Customer 360, Product 360, Document Intelligence, predictive ML and governed AI workflow automation.

Nexora is an original product. It takes functional inspiration from publicly described enterprise data and AI service capabilities in the industry. It copies no third-party branding, code, design or intellectual property.

## 2. Problems solved

| Problem | Who has it | Nexora answer |
|---|---|---|
| Data is spread across CSV exports, spreadsheets and operational databases, with no single trustworthy copy | Data/BI teams | Data Engineering Hub: connectors, pipelines, quality reports, lineage |
| Manual, error-prone data cleaning and deduplication | Analysts, operations | Profiling, validation rules, duplicate detection, reusable transformation pipelines |
| Executives cannot query their own data without an analyst | Business leaders | Conversational BI: natural language to **guarded** SQL, with charts and explanations |
| No visibility into who accessed what data, and why | Compliance, security | Governance: RBAC, column and row policies, audit logs, access history, retention |
| Customer records fragmented across CRM, web, app, loyalty and sales systems | Marketing, CX | Customer 360: identity resolution, unified profiles, segmentation, churn and CLV (post-MVP) |
| Inconsistent product data across suppliers and marketplaces | Retail, e-commerce | Product 360: SKU normalization, attribute extraction, reviewable corrections (post-MVP) |
| Thousands of documents processed by hand | Operations, finance, legal | Document Intelligence: OCR (English, Hindi, Gujarati), extraction, RAG Q&A with citations (post-MVP) |
| Predictive models are built ad hoc, with no versioning or monitoring | Data science | ML Workspace: EDA, model recommendation, training, evaluation, registry, monitoring (post-MVP) |
| Repetitive, approval-heavy business processes | Operations | Workflow automation with tool-scoped agents and human approval (post-MVP) |

## 3. Personas

| Persona | Primary goals | Typical role in Nexora |
|---|---|---|
| **Platform Super Admin** (Nexora operator) | Platform health, tenant oversight, Gemini quota, background jobs | `platform_admin` (outside all orgs) |
| **Org Owner / Org Admin** | Set up the workspace, invite users, connect data, set policies, consent to AI data use | `owner`, `admin` |
| **Data Engineer** | Build pipelines, monitor runs, fix quality issues | `data_engineer` |
| **Business Analyst** | Explore datasets, ask questions, build dashboards | `analyst` |
| **Executive / Business User** | Ask questions in plain language, read dashboards | `viewer` (query-enabled) |
| **Data Scientist** (post-MVP) | Train and compare models, publish predictions | `data_scientist` |
| **Compliance Officer** | Review audit logs, access history, retention | `auditor` (read-only governance) |

## 4. Product principles

1. **Security before features.** Tenant isolation and guarded SQL execution are designed in, not added later.
2. **AI is a scarce, governed resource.** Gemini is the only external generative-AI provider. The architecture minimizes calls (caches, deterministic paths, local processing) and never silently switches to another provider.
3. **Synthetic data on the free tier.** The default per-org `AIDataPolicy` is `synthetic_only`. Real customer records and confidential data never reach the Gemini free tier (see `AI_ARCHITECTURE.md` §5).
4. **Transparency.** Every AI answer shows the executed SQL, sources, assumptions and citations. Users can always see what happened.
5. **Humans approve high-impact actions.** Identity merges, product corrections, agent actions and policy changes are reviewable.
6. **Local first.** Profiling, quality checks, dedup, OCR, embeddings and ML training run on open-source libraries locally.
7. **Honest status.** A feature is "Working" only when its acceptance tests pass in CI. A visible UI does not count as done.

## 5. Scope: MVP vs later

### 5.1 Minimum commercially useful product (MVP = Phases 1–5 plus basic slices of 11 and 12)
1. **Secure organization and user management:** sign-up and login, orgs, invitations, roles, RBAC, tenant isolation (RLS), audit log.
2. **Data ingestion and data quality:**
   - CSV, Excel, JSON and PostgreSQL sources;
   - schema detection and profiling;
   - validation and cleaning rules, duplicate detection, transformations;
   - linear pipelines with scheduling, retries and run history;
   - quality reports and basic lineage.
3. **Conversational BI:**
   - semantic layer and guarded NL→SQL;
   - streaming chat, charts and tables, follow-ups, query history;
   - exports and a SQL transparency panel.
4. **AI-powered business insights:** locally computed insights (top-N, period-over-period, anomalies, trends), template narration, and optional Gemini narration where policy allows.
5. **Enterprise governance:**
   - dataset ACLs, column and row policies, PII tagging;
   - access history, retention policies;
   - API and AI usage monitoring, plus AI data-policy consent.
6. Basic **Org-Admin** and **BI dashboards**, and a CI **security baseline** (isolation suite, SQL attack corpus).

### 5.2 After the MVP
- **Recommended order:** Document Intelligence (P8) → ML Workspace (P9) → Deployment (P13) → Customer 360 (P6) → Product 360 (P7) → Automation and Agents (P10).
- Super-admin monitoring (P11b) and full hardening (P12b) follow.

### 5.3 Non-goals for the initial product
- Microservices, Kubernetes or multi-region deployment.
- Real-time streaming ingestion (Kafka/CDC). Batch and scheduled only.
- Any external LLM other than Google Gemini, including as a fallback.
- Fully autonomous agents with unrestricted access to enterprise systems.
- Processing real personal or confidential data on the Gemini free tier.
- Snowflake, BigQuery, Salesforce and other enterprise connectors. They are planned behind the connector SDK.
- SSO (SAML/OIDC). Planned for later.

## 6. Success metrics (MVP)

| Area | Metric | Target |
|---|---|---|
| Security | Cross-tenant access attempts that succeed in the automated isolation suite | **0** |
| Security | SQL attack corpus rejected or neutralized | **100%** |
| BI accuracy | Golden NL question set: execution-correct answers (synthetic sales dataset) | **≥ 80%** |
| Data engineering | 1M-row CSV ingested, profiled and quality-checked on the dev machine | **< 2 min** |
| Reliability | Failed pipeline runs that retry and record a structured error | **100%** |
| AI efficiency | Share of repeated or similar questions answered without a Gemini call (cache or builder) | **≥ 50%** in the demo scenario |
| Transparency | AI answers that show the executed SQL and an audit entry | **100%** |
| Quality bar | Modules marked "Working" without passing acceptance tests | **0** |

## 7. Feature status legend

| Status | Meaning |
|---|---|
| **Working** | Implemented, with acceptance tests passing in CI |
| **Prototype** | Implemented, but with limited tests, scale or edge-case coverage |
| **Mock** | UI or demo data only; no real backend behaviour |
| **Planned** | Designed in these documents, not built |

The live status of every module is in `FEATURE_STATUS.md`. The UI shows a badge on every Prototype and Mock module.

## 8. Constraints and assumptions

- **Builder:** a single developer using Claude Code, with parallel agent waves (see `AGENT_DEVELOPMENT_STRATEGY.md`). About 4–6 months available.
- **Dev environment:** Windows 11, Python 3.12, Node 24, Docker Desktop, PostgreSQL 17. The server-side stack runs in Docker Compose, because Celery has no official Windows support.
- **AI:** Google Gemini API only (`google-genai` SDK), free tier first. Quotas are small, set per Google Cloud project, and change without notice (see `AI_ARCHITECTURE.md` §2).
- **Languages:** the UI is in English. Document Intelligence targets **English, Hindi and Gujarati**.
- **Paid services** are flagged explicitly in `DEPLOYMENT_ARCHITECTURE.md` §9. Nothing in the MVP *requires* a paid AI provider.

## 9. Document map

| Document | Purpose |
|---|---|
| `SYSTEM_ARCHITECTURE.md` | Modular monolith, processes, app boundaries, technology rationale |
| `DATABASE_SCHEMA.md` / `DATABASE_ERD.md` | Every table, its constraints, RLS, and the ER diagrams |
| `API_SPECIFICATION.md` | REST and SSE contracts |
| `DATA_ENGINEERING_DESIGN.md` | Connectors, ingestion, quality, pipelines, lineage |
| `AI_ARCHITECTURE.md` | Gemini service, quota, data policy, NL→SQL, RAG, agents |
| `ML_ARCHITECTURE.md` | ML workspace |
| `SECURITY_ARCHITECTURE.md` | Threat model and controls |
| `MULTI_TENANCY_DESIGN.md` | Tenancy model and RLS mechanics |
| `UI_UX_SPECIFICATION.md` | Design system and screens |
| `TESTING_STRATEGY.md` | Test pyramid and acceptance testing |
| `DEPLOYMENT_ARCHITECTURE.md` | Compose, CI/CD, operations, paid services |
| `IMPLEMENTATION_ROADMAP.md` | The 13 phases in detail |
| `AGENT_DEVELOPMENT_STRATEGY.md` | Parallel agent waves, ownership, contracts |
| `FEATURE_STATUS.md` | Live status matrix |
