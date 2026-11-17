# Feature Status

> Maintained by the Integrator. **A module becomes Working only when its acceptance tests pass on `main`.**
> Statuses:
> - **Working**: implemented and tested.
> - **Prototype**: implemented, but with limited tests or scale.
> - **Mock**: UI or demo data only.
> - **Planned**: not built.

| Area | Module | Status | Evidence / notes |
|---|---|---|---|
| Foundation | Monorepo, Docker Compose, Django project, all apps pre-wired, Celery queues | Prototype | Wave 0. Compose not yet verified running (Docker was not running) |
| Foundation | Tenancy core (`app.org_id` RLS, fail-closed managers, `TenantAPIView`, `TenantTask`) | Prototype | Wave 0 code; tests pending (QA wave 1) |
| Foundation | Audit log, jobs + SSE (Redis Streams), object storage helpers | Prototype | Wave 0 code |
| Foundation | CI (conventions, ownership, ruff, import-linter, pytest, frontend, gitleaks) | Prototype | Workflow written; not run on GitHub (no remote) |
| W1 | Auth (signup, login, sessions, CSRF), orgs, members, invitations, roles, API keys | Planned | Backend agent, wave 1 |
| W1 | AI data policy (default `synthetic_only`, consent for `schema_only`) | Prototype | Default enforced in model + DB default; consent API in wave 1 |
| W1–2 | File ingestion, schema detection, profiling, quality, warehouse storage | Planned | Data Engineering agent |
| W2 | PostgreSQL connector, pipelines, lineage, semantic layer, SQL guard | Planned | |
| W1–3 | Gemini service, quota and caching, NL→SQL, insights | Planned | AI/ML agent |
| W3 | Conversational BI end to end | Planned | |
| W4 | Governance, analytics, admin console | Planned | |
| W5 | Customer 360, Product 360, Document Intelligence | Planned | |
| W6 | ML workspace, workflow automation | Planned | |
| W7 | Hardening, performance, production readiness | Planned | |
| UI | Design system, app shell, dashboard | Planned | Frontend agent, wave 1 |
