# Handoff — qa_security — wave 1
Branch: agent/qa_security/w1   Base: fd484d9   Contract version: W1 (contracts/api/CONTRACT.md as of fd484d9)

## Done
- **Shared test infrastructure** (`backend/testing/`, loaded for every agent via `pytest_plugins`)
  - `fixtures.py`: `make_user`, `make_org`, `org_world`, `two_orgs` (orgs `acme-analytics` / `beta-retail`,
    each with owner + analyst + viewer, plus an `outsider` with no membership; extra roles on demand via
    `world.user("auditor")`), `api_client_for(user, csrf=True)`, `anon_client`, `tenant(org)`, `fake_gemini`,
    `network_spy`, `fake_redis`, `fake_clock`, `canaries`.
  - `factories.py` (orgs via the real `create_organization`), `client.py` (CSRF-enforcing `CsrfAPIClient`:
    `enforce_csrf_checks=True` + csrftoken cookie + `X-CSRFToken`; uses `GET /api/v1/auth/csrf` once it exists),
    `db.py` (RLS/GUC probes, `clear_db_tenant`, `independent_connection`), `routes.py` (URLconf enumeration),
    `isolation.py` (per-route resource factories, `MissingIsolationFixture`), `fakes.py` (`FakeGeminiService`
    implementing `nexora.contracts.GeminiService`, `NetworkSpy`, `FakeRedis`, `FakeClock`), `security.py`
    (canaries, formula-injection oracle), `sqlguard.py` (corpus loader + independent sqlglot oracle).
- **Tenant isolation** (`backend/tests/isolation/`) → AC P2-AC2
  - `test_endpoint_matrix.py`: every route under `/api/v1/orgs/<org>/` discovered from the live URLconf
    (new endpoints are covered automatically): non-members (A's owner/analyst/viewer + outsider) on B's slug get
    404 on every handled method with B's REAL resource ids; anonymous never 2xx; IDOR (B's owner, B's slug,
    A's ids) → 404; list bodies scanned for A's identifiers; unsafe methods without CSRF → 403.
  - `test_rls_database.py`: for EVERY public table with `org_id` (+ `orgs_organization`): RLS enabled + forced,
    canonical `tenant_isolation` USING/WITH CHECK, no extra permissive policies, 0 rows with no context,
    no foreign rows under B, generic cross-org INSERT (row clone via jsonb) fails with 42501, moving a row to
    another org fails, app role cannot disable RLS / SET ROLE, SECURITY DEFINER lookups only answer for the caller.
  - `test_static_tenancy.py`: every `TenantModel` has `EnableTenantRLS` in its migrations, unique constraints
    include `org`, no global `unique=True`, tenant routes use `TenantScopedMixin`.
  - `test_tenancy_core.py`: context nesting / `TenantMismatch` / rollback, fail-closed manager on every tenant
    model, ORM filter and RLS each isolate on their own, `TenantTask` (kwarg org_id, eager `.delay`, mismatch,
    inactive/unknown org, long_running + `tenant_step`), GUC is transaction-local on a reused connection and never
    visible on another connection, no session-level tenant setting anywhere in `apps/`.
  - `test_jobs_sse_isolation.py`: stream keys derived from the active org (an A worker cannot write B's stream),
    idempotency keys per org, SSE access matrix + HTTP 401/404, SSE streams only the requested org's events
    (through the real view with an async fake Redis), `jobs.view_all` visibility.
- **Security** (`backend/tests/security/`)
  - `sql/corpus.yaml`: **118 cases** in 9 categories (smuggling 23, dangerous functions 22, cross-tenant/catalog
    16, denied columns 22, row-policy escapes 9, lexical 12, DoS 6, prompt injection 4, positive controls 4) with
    `reject` + accepted codes / `neutralized` / `safe` verdicts and 3 policies (default, masked, north_only).
    `test_sql_guard_corpus.py` runs them through `apps.sqlguard.services.validate(sql, policy)` and checks every
    accepted rewrite with an independent oracle (tables ⊆ policy, no denied column, row predicate on every
    reference); `test_sql_guard_fuzz.py` Hypothesis fuzzing (`NEXORA_FUZZ_EXAMPLES`, default 300). Guard tests
    skip with a clear reason until wave 2; corpus structure, oracle self-tests and strategies run now. → P4-AC1
  - `test_ai_policy.py` → P2-AC3: service path, raw INSERT uses the DB default, column default declared, CHECK
    rejects unknown values, `ai_policy_state()` contract (manual DB edit without consent is not effective);
    signup / `POST /orgs` / PATCH / consents (free tier → `ai_policy_tier_forbidden`, acknowledged, stale
    version, non-admin 403, re-auth, consent→revoke, cross-org) and the AI gate (8 policy/payload/classification
    cases + no network) activate automatically when the routes/functions merge.
  - `test_uploads.py` (zip bomb, oversize, magic bytes, hostile CSVs, cross-org, formula cells ingested verbatim),
    `test_secrets_in_logs.py` (canaries in logs, audit rows, readyz errors, login/signup/api-key flows),
    `test_storage_keys.py`, `test_crypto.py` (round trip, AAD binding, tamper on every field, wrong KEK).
- **Integration** (`backend/tests/integration/`): audit append-only (trigger + revoked UPDATE/DELETE/TRUNCATE),
  redaction, actor/target, audit API permission + org scoping; migration operations vs the PG schema editor;
  testdata determinism and properties.
- **AI eval** (`backend/tests/ai_eval/test_fakes.py`): self-tests of the shared fakes. Cassettes dir reserved.
- **Test data** (`testdata/generate.py`, stdlib only, seed 20261117 + `--anchor`): `sales.csv` (5,020 rows =
  5,000 orders + 20 exact duplicates, 24 months to the anchor month incl. current partial month, INR, festive
  seasonality, planted anomaly week, nulls), customers CRM/web + ground truth (350 messy matches), suppliers A/B
  + ground truth (139 matches, unit conversions), `churn.csv` + `churn_meta.json` (oracle AUC 0.8132),
  adversarial files (malformed, formula injection, 1 MiB cell, 2,000 columns, zip-bomb xlsx ratio 293,
  PNG/MZ/text with wrong extensions). Committed under `testdata/generated/` with `MANIFEST.json` (sha256) and
  `testdata/.gitattributes` (`-text`, byte-exact). README documents everything.
- **E2E** (`e2e/`): Playwright 1.64.1 + @axe-core/playwright 4.14.0, `baseURL` http://localhost:8080,
  `tests/smoke.spec.ts` (UI signup → dashboard → datasets → upload sales.csv → preview → load → dataset detail;
  unknown org → 404), `tests/accessibility/a11y.spec.ts` (WCAG 2.1 A/AA, desktop + 375 px). `npm install` done in
  `e2e/` only; `tsc --noEmit` clean. Global setup fails fast (or skips with `E2E_SKIP_IF_DOWN=1`).
- **Perf** (`perf/k6/`): `upload_ingest.js`, `query.js`, shared `lib/api.js` (CSRF/session like the SPA). Not run.

## Tests (real counts)
Shared Docker infra (localhost:5433/6380/9000) was **down** and the images were not present. To still execute the
DB suites I ran them against a **private, throw-away PostgreSQL 17 cluster** (local install, port 55433, roles and
template grants replicated from `infra/postgres/init/00-init.sh`; pgvector not installed — no current migration
needs it). Redis/MinIO were not needed (fakes / monkeypatched clients).
- Command: `$env:PYTEST_DB_SUFFIX="qa_security"; python -m pytest tests --create-db -p no:cacheprovider`
  (+ `DATABASE_URL_*` pointing at :55433).
- **Without a fix for the MakeAppendOnly defect (below) `migrate` fails on every fresh DB:** 71 passed,
  123 skipped, 5 xfailed, **131 errors** (all DB tests error at setup). This blocks every agent's DB tests.
- With a LOCAL-ONLY shim that passes that SQL with `params=None` (not committed): **`tests/`: 165 passed,
  160 skipped, 5 xfailed, 0 failed** (also identical under `-n 2` as CI runs it).
  `tests/ + apps/`: 171 passed, 160 skipped, 5 xfailed, **1 failed** = `apps/core/tests/test_tenancy.py::
  test_no_context_sees_zero_rows_and_manager_fails_closed` (caused by the GUC-leak defect below).
- Skips (160), each with a reason: 122 SQL-guard corpus/fuzz (`validate()` not shipped), 23 AI-policy
  endpoints/gate, 3 secrets endpoint flows, 11 uploads, 1 empty parametrization (no unsafe tenant routes yet).
- 5 strict xfails = confirmed defects (they turn red when fixed so the marker gets removed).
- `scripts/check_conventions.py`: conventions OK. `scripts/check_ownership.py --agent qa_security`: OK.
  `ruff check testing tests`: clean.
- E2E: `npx playwright test` with `E2E_SKIP_IF_DOWN=1` → 10 skipped (no stack running); not executed against a
  server. k6: syntax-checked only, not executed.

## Defects found in others' code (QA only)
1. **backend (BLOCKER)**: `backend/apps/core/tenancy/operations.py:81` (SQL at :86) — `MakeAppendOnly` passes
   plpgsql containing `RAISE EXCEPTION 'table % is append-only'` to `schema_editor.execute(sql)` with the default
   `params=()`; Django's PG schema editor runs `compose_sql(sql, ())` → psycopg `incomplete placeholder: '%'`, so
   `manage.py migrate` / `pytest --create-db` fail on any fresh database. Fix: `schema_editor.execute(sql, None)`
   (or `%%`). Repro: `backend/tests/integration/db/test_migration_operations.py` (and every DB test errors).
2. **backend**: `backend/apps/core/tenancy/context.py:79-84` — `tenant_context()` does not restore the DB tenant
   setting on exit when nested in an outer transaction (transaction-local `set_config` survives the savepoint
   release), so code after the context in the same transaction still sees that org's rows through RLS (request
   transactions, `create_organization` called inside an atomic block, every pytest-django test). Fix: remember
   `current_setting(TENANT_GUC, true)` on entry and restore it on exit. Repro:
   `tests/isolation/test_tenancy_core.py::test_guc_is_cleared_after_context_exit_inside_outer_transaction`
   (strict xfail); also makes `apps/core/tests/test_tenancy.py:43` fail.
3. **backend**: `backend/apps/core/tenancy/tasks.py:52` — `TenantTask.__call__` sets `current_actor` from
   `actor_id` and never resets it; the next task on the same worker thread inherits the previous user (wrong
   audit attribution). Repro: `tests/isolation/test_tenancy_core.py::test_tenant_task_does_not_leak_actor_to_later_tasks`.
4. **backend**: `backend/apps/core/tenancy/views.py:76-78` — `finalize_response` calls
   `transaction.set_rollback(True)` whenever `connection.in_atomic_block`, even when the view's own tenant
   transaction was never entered (non-member 404, anonymous). Under any outer transaction it poisons the
   caller's transaction; the session save then fails and the client gets 400 instead of 404. Fix: only roll back
   when the mixin entered its atomic block (track a flag). Repro:
   `tests/isolation/test_endpoint_matrix.py::test_non_member_404_does_not_poison_callers_transaction`.
   (The shared `CsrfAPIClient` runs each request in its own savepoint so other agents' API tests behave like
   production meanwhile.)
5. **backend**: `backend/apps/core/storage.py:44` (and `org_key` at :37) — prefix check is a plain `startswith`,
   so `orgs/<A>/../<B>/f` passes; `org_key("..", ...)` builds such keys. Reject `.`/`..`/empty segments and
   backslashes. Repro: `tests/security/test_storage_keys.py::test_check_key_rejects_path_traversal_segments`.
6. **backend**: `backend/apps/core/storage.py:79` — `presigned_get(filename=...)` interpolates the filename into
   `ResponseContentDisposition` unescaped (quotes/CR/LF/`filename*` injection). Use RFC 6266 encoding
   (`filename*=UTF-8''<pct-encoded>` + sanitised ASCII fallback). Repro:
   `tests/security/test_storage_keys.py::test_presigned_filename_is_sanitised`.

## Requests (Integrator)
- [infra/conftest] Transactional tests (`django_db(transaction=True)`, live server, Celery-eager-off
  integration) cannot run: flush as `nexora_app` fails (`permission denied for table core_job`, no TRUNCATE).
  Suggest flushing via the migrator/owner connection in `backend/conftest.py` (or a test-only TRUNCATE grant).
- [process] Announce to all agents: every new tenant route with resource kwargs needs an isolation factory in
  `apps/<app>/tests/isolation.py` (`ISOLATION_FACTORIES = {"<route-name>": fn(world) -> {kwarg: id}}`,
  `KWARG_FACTORIES = {"dataset_id": fn}`, or `ISOLATION_EXEMPT = {"<route-name>": "reason"}`); otherwise
  `test_every_resource_route_has_an_isolation_factory` fails (TESTING_STRATEGY §4.1 `MissingIsolationFixture`).
- [settings, optional] `backend/tests/__init__.py` is unowned; QA subpackages carry their own `__init__.py`
  so pytest imports them as `isolation.*`, `security.*`, `integration.*`, `ai_eval.*`. If you prefer
  `tests.*` module names, add an empty `backend/tests/__init__.py` (works either way).
- [CI] Add `e2e` (`npm ci && npx playwright install --with-deps chromium && npm test`) to the PR-to-main job and
  set `NEXORA_FUZZ_EXAMPLES=10000` for the nightly SQL fuzz run.
- [contract] For the SQL corpus: Data Engineering's `validate()` should use the reject codes listed in
  SECURITY_ARCHITECTURE §5.1 (the corpus accepts the documented alternatives per case).

## Open issues / known limitations
- Shared infra was down; DB suites were verified only on the private PG17 cluster (no pgvector) with the local
  migration shim for defect 1. Re-run on the shared stack after defect 1 is fixed.
- SQL corpus `stage: execute` cases (6-way cross join cost) and the EXPLAIN differential check need the
  warehouse executor (wave 2/3). Export formula-escaping tests need the W3 exports flow; the oracle
  (`testing.security.unescaped_formula_cells`) is ready.
- E2E selectors follow UI_UX_SPECIFICATION (roles/labels, `/o/<slug>/…`); adjust once the frontend lands if
  labels differ. Not run against a server.
- `test_ai_policy.py` gate tests detect `check_policy(org_id, payload)` vs `check_policy(payload)` by signature;
  AI/ML should keep one of these shapes.
