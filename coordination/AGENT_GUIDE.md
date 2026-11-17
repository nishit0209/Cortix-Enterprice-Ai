# Agent Guide (read before coding)

> Integrator-owned. Applies to every agent in every wave.

## Environment
- **Repo root:** `D:\nexora enterprice ai`. You work in your own **git worktree**. Edit only files inside your ownership globs (`coordination/OWNERSHIP.md`).
- **Python:** use the shared venv interpreter `"D:\nexora enterprice ai\.venv\Scripts\python.exe"`. Never `pip install` into it. If you need a dependency, add a request to your handoff.
- **Backend tests:** run from `<your worktree>/backend`.
  - Command: `$env:PYTEST_DB_SUFFIX="<your_role>"; & "D:\nexora enterprice ai\.venv\Scripts\python.exe" -m pytest <paths> --create-db`
  - Tests run as the `nexora_app` DB role, so **RLS is enforced**.
  - Postgres runs in Docker at `localhost:5433` (pgvector/pg17); Redis is at `localhost:6380`.
  - If Docker is down, still write the tests, and report them as "not executed: DB unavailable".
- **Settings** come from `backend/nexora/settings/*.py` (Integrator-owned) and are read from env. All config keys already exist in `.env.example`.
- **The frontend** runs with `npm` inside `frontend/`. Install dependencies only in your worktree's `frontend/`.

## Architecture rules (non-negotiable)
1. **Tenancy:**
   - Every tenant-owned model subclasses `apps.core.tenancy.models.TenantModel` (UUIDv7 `id`, `org` FK, `created_at`, `updated_at`, fail-closed `objects`).
   - Every tenant model's migration adds `EnableTenantRLS("<app_label>_<modelname>")` from `apps.core.tenancy.operations`.
   - Every `UniqueConstraint` includes `org`.
   - Append-only tables add `MakeAppendOnly("<table>")`.
2. **Views:**
   - Tenant APIs subclass `apps.core.tenancy.views.TenantAPIView` / `TenantGenericViewSet` / `TenantModelViewSet`.
   - Routes go in your app's `urls.py`, which is already mounted under `/api/v1/orgs/<org>/`. Use `SimpleRouter(trailing_slash=False)`.
   - The URL kwarg `org` is consumed by the tenancy mixin.
   - Permissions: `permission_classes = [IsAuthenticated, HasOrgPermission]` with `required_permissions` using codenames from `apps/orgs/permissions_catalog.py`. Need a new codename? Request it.
3. **Tenant context outside requests:**
   - `with tenant_context(org_id): ...`.
   - Celery tasks: `@shared_task(base=TenantTask)` with **keyword** `org_id=`.
   - Set `long_running = True` and use `apps.core.tenancy.tasks.tenant_step(org_id)` to commit in steps.
4. **Never** write the literal tenant setting string; use `TENANT_GUC`. **Never** use `.unscoped` outside platform modules, or `.objects.raw()` / `.extra()`.
5. **Gemini:** only `apps/ai` may import `google.genai`. Gemini is the ONLY LLM provider: no other SDKs, no fallback provider.
6. **AI data policy:**
   - The default is `synthetic_only`.
   - Only datasets/documents classified `synthetic` or `public` may be sent to Gemini, unless the org has a valid consent for `schema_only`, which allows SCHEMA only.
   - `aggregates_only` and `full` are refused when `GEMINI_TIER=free`.
   - **Tests must never call Gemini live.** Use fakes (`backend/testing/` once QA provides them, or local fakes in your app's tests).
7. **Shared services:**
   - `apps.core.audit.record(action, target=..., metadata=...)` for every mutation.
   - `apps.core.jobs.create_job/update/finish/fail/publish` for async work. The SSE endpoint `/orgs/<org>/jobs/<id>/events` already exists.
   - `apps.core.storage` for MinIO, with keys `orgs/<org_id>/...` via `org_key(...)`.
   - `apps.core.exceptions.NexoraError` subclasses produce problem+json.
8. **Cross-app calls:** only through the other app's `services.py`, or the contracts in `backend/nexora/contracts.py`. Layer order: core ← accounts/orgs/ai ← governance ← data apps ← nl2sql/documents/ml ← bi/customers/products/automation/platform_admin. Lower layers never import higher ones; use `apps.core.signals` or a registry.
9. **API shapes:** `contracts/api/CONTRACT.md` is binding. If you must deviate, document it in your handoff.
10. **Synthetic data only** in tests and fixtures. No real personal data.

## Definition of done (per task)
Done means the code is implemented, migrations are generated (`manage.py makemigrations <app>`, run with `DJANGO_SETTINGS_MODULE=nexora.settings.test`) with RLS operations added, tests are written and run (report the results honestly), and `python scripts/check_conventions.py` passes.

## Git
- Inside your worktree: `git checkout -b agent/<role>/<wave>`.
- Commit often with clear messages. **Never** merge, rebase onto main, push, or touch other branches.
- End with your handoff file `coordination/handoffs/<wave>/<role>.md` (template in `docs/AGENT_DEVELOPMENT_STRATEGY.md` §7), committed on your branch.
