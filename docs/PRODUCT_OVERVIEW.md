# NEXORA Enterprise AI: Product Overview

> **What it is:** NEXORA is a multi-tenant **Enterprise AI and Data Intelligence platform**. A company connects its data, cleans and governs it, then asks business questions in plain language and gets charts and insights. On the same platform it runs Customer 360, Product 360, document intelligence, machine learning and automated workflows with human approvals.
>
> **Who it is for:** mid-size and large organizations, in particular:
> - **business teams:** sales, operations, finance;
> - **data and analytics teams:** data engineers, analysts, data scientists;
> - **IT and security owners**, who need AI on company data without losing control of it.
>
> **Delivery model:** SaaS (many organizations on one platform, fully isolated) or a private install on the customer's own server.

---

## 1. The problem NEXORA solves
- **Scattered data.** Company data is spread across CSV/Excel files, databases and documents. Cleaning it, joining it and trusting it takes weeks.
- **Analyst dependency.** Business users can't get answers without an analyst writing SQL.
- **Unsafe AI tools.** Generic AI chat tools are unsafe on company data: they leak confidential records, can't be audited and ignore access rights.
- **Too many tools.** Customer, product, document and ML tools are separate products that don't share data, security or governance.

**NEXORA puts all of this in one secure platform:** one login, one data layer, one governance model, one audit trail.

## 2. Product modules (A–J)

### A. Executive Dashboard
- KPI cards (datasets, rows, data quality, pipelines, runs, AI usage) with period-over-period change.
- Customizable dashboards with drag and resize widgets (charts, tables, KPIs, text).
- **Pin any AI answer to a dashboard.** Widgets re-run live under each viewer's own access rights.
- AI insights feed, recent queries and an activity feed.

### B. Data Engineering Hub
- **Data sources:**
  - upload CSV, Excel or JSON;
  - connect an external PostgreSQL database (read-only access is verified, credentials are encrypted, connections to internal or private network addresses are blocked).
- **Automatic profiling:** column types, statistics, missing values, distributions and **PII detection** (emails, phones, IDs).
- **Data quality:** rules, a quality score (0–100), duplicate detection and quality reports.
- **Visual pipeline builder:** a drag-and-drop canvas with 13 transform steps (select, rename, cast, filter, fill nulls, drop nulls, deduplicate, trim, change case, derived columns, sort, aggregate, join), preview, schedules, retries and run history.
- **Data lineage** graph, from source through pipeline to dataset.
- **Semantic layer:** business names, descriptions, synonyms and metric definitions, generated automatically and editable.

### C. Conversational BI (flagship)
- Ask questions in plain English, for example "total revenue by region last quarter", and get a **table, an auto-selected chart and written insights**.
- **Live streaming answers:** the user watches each stage (understanding → SQL → validation → running → explaining).
- **Full transparency:** the exact SQL that ran is always shown. Power users get a manual SQL editor.
- **A SQL safety guard on every AI-generated query:**
  - read-only queries, with only allowed tables, columns and functions;
  - masked and hidden columns enforced, row-level filters applied;
  - a cost check, timeouts and row limits.
- Follow-up questions with conversation memory, **query history**, saved queries, feedback (👍/👎) and **CSV/Excel export** with protection against spreadsheet formula injection.
- **Answers without spending AI quota:** a smart cache and a built-in query builder answer common questions without calling Gemini. On the demo sales data the builder alone answers about 45 of 48 benchmark questions correctly.

### D. Customer 360
- **Customer sources:** map any dataset's columns to customer fields (email, phone, name, address, ID).
- **Identity resolution:** finds the same customer across sources using fuzzy matching. Confident matches merge automatically; uncertain ones go to a **human review queue**.
- **A unified customer profile** with a timeline (orders, tickets, events).
- **Segments:** RFM (recency, frequency, monetary) plus rule-based segments.
- **Scoring:** churn risk and customer lifetime value, using the ML workspace, with explanations and next-best-action suggestions.

### E. Product 360
- SKU normalization, brand cleanup and unit parsing (500 ml = 0.5 L).
- **Attribute extraction** (size, color, pack count, material).
- **Duplicate and variant detection** across supplier catalogs, with a review queue.
- **Corrections workflow:** changes are applied only after a reviewer approves them.
- A product quality score and **semantic product search**.

### F. Document Intelligence
- Upload PDF, Word, Excel, CSV or text. The platform extracts text, tables and pages, with **OCR for scanned pages in English, Hindi and Gujarati**.
- **Search:** keyword and meaning-based ("semantic") search, combined.
- **Ask your documents:** answers **with citations** (document, page, quote).
- Summaries, entity extraction (amounts, dates, IDs, organizations), document classification (invoice, contract, resume, …), structured extraction, document comparison, and promoting a document table to a dataset.
- Embeddings run **locally** with a multilingual model, so no document text is sent out for search.

### G. ML Workspace
- **A guided wizard:** pick a dataset, then a target, then features. It automatically detects the task: classification, regression, forecasting or clustering.
- **Automatic EDA:** statistics, missing values, correlations, class balance.
- **Model recommendations with plain-English reasons.**
- **Training on CPU:** Logistic/Linear, Random Forest, XGBoost and Gradient Boosting; forecasting with ExponentialSmoothing/SARIMAX; clustering with KMeans. Cross-validation runs automatically.
- **Metrics and comparison:** accuracy, F1, ROC-AUC, RMSE, R², MAPE and more, plus a confusion matrix, feature importance and **SHAP explanations**.
- **Model registry:** versions, plus staging and production stages, where promotion to production needs an approval.
- **Prediction:** a playground in the UI; a **Prediction API** using API keys; batch prediction that writes a new dataset.
- **Drift monitoring** (PSI per feature).
- **Built-in safeguards:**
  - leakage prevention (ID-like and target-copy columns are dropped);
  - columns the user isn't allowed to see are never used as features;
  - model files are integrity-checked before loading.

### H. Workflow Automation with human approvals
- **A visual workflow builder:** steps such as run a pipeline, run a saved query, train or score a model, resolve customers, notify, wait, add a condition, or require approval.
- **Triggers:** schedule (cron), webhook (signed and protected against replay), and events (dataset ready, pipeline failed, document processed).
- An **approvals inbox**. High-impact actions require re-entering the password.
- **AI agents:** limited to an allow-list of tools, with every action audited.

### I. Governance and Security
- **Dataset access control** per user and role.
- **Column policies:** allow, deny or mask (hide fully, hash, or show only the last characters).
- **Row policies:** per-user filters, for example "region = my region".
- A **policy preview** shows exactly what a given user will see.
- **Data retention policies**, with a dry run before applying.
- **Access history** and a full, tamper-resistant **audit log**.
- **AI data policy:** controls what is ever sent to Gemini (details in section 3).

### J. Admin Console
- **Organization admin:** members, invitations, roles and permissions, API keys, MFA, AI usage and budgets, and a usage analytics dashboard.
- **Platform super-admin**, for the SaaS operator:
  - all organizations (counts only, never their data), with suspend and reactivate;
  - system health, background jobs, AI usage and quotas, and logs.

---

## 3. What makes NEXORA different (selling points)
1. **AI that respects company data rules.**
   - Every AI request passes an **AI data policy gate**. The default, *synthetic_only*, never sends real company data to the AI provider.
   - Sending even table and column names (*schema_only*) requires explicit admin consent.
   - Sending real data needs a paid, enterprise-grade AI contract.
2. **Strict multi-tenant isolation.**
   - Each organization's data is isolated **inside the database** (PostgreSQL Row-Level Security). Isolation does not rely on application code alone.
   - Each organization's warehouse data lives in its own schema, behind its own read-only role.
3. **Safe AI-generated SQL.** Several layers protect it: a SQL guard, rewriting with masks and filters, a read-only database role, a cost check, timeouts and an audit trail.
4. **One platform instead of many:** BI, Customer 360, Product 360, documents, ML and automation share one data layer, one security model and one audit log.
5. **Works offline-first:** profiling, quality checks, document search, insights and ML all run **locally**. The AI provider is used only where it adds value, and within quota limits.
6. **Single AI provider by design:** Google Gemini only. That gives one data-processing agreement and one set of terms.
7. **Multilingual:** English, Hindi and Gujarati, for OCR and search.
8. **Light infrastructure:** runs on **PostgreSQL alone**. Redis, object storage and pgvector are optional accelerators.

## 4. What is in the box
| Part | Technology |
|---|---|
| Web application | Next.js (React, TypeScript), Tailwind, light and dark themes |
| API and backend | Django + Django REST Framework, real-time streaming (SSE) |
| Database | PostgreSQL: app database plus a separate analytics warehouse database |
| AI | Google Gemini (only), with quota management, caching and a circuit breaker |
| Local AI and ML | sentence-transformers (multilingual embeddings), scikit-learn, XGBoost, statsmodels, SHAP, Tesseract OCR |
| Background work | Built-in task runner and scheduler (Celery + Redis optional for scale) |
| Setup | One command: `scripts/setup_local.py` creates the databases, users and tables |
| Demo data | `manage.py seed_demo` loads a realistic synthetic company (sales, customers, products, orders, tickets) |
| Tests | Thousands of automated tests, including tenant-isolation, SQL-attack and security suites, run against a real PostgreSQL |

## 5. Current status
The live, honest status of every module is in **[FEATURE_STATUS.md](FEATURE_STATUS.md)**.
- **Working:** tested end to end on a real database.
- **Prototype:** built, with integration still being completed.
- **Planned:** not built yet.

**Before selling to real customers**, see the checklist in the final run guide:
- production deployment (server, HTTPS, Docker);
- a paid Gemini plan for real customer data;
- legal documents (terms of service, privacy policy, data-processing agreement);
- load testing at the customer's scale.

## 6. Related documents
- [PROJECT_VISION.md](PROJECT_VISION.md): personas, principles and success metrics.
- [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md), [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md), [AI_ARCHITECTURE.md](AI_ARCHITECTURE.md), [ML_ARCHITECTURE.md](ML_ARCHITECTURE.md): how it is built.
- [MASTER_PLAN.md](MASTER_PLAN.md) and [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md): how it was planned and delivered.
