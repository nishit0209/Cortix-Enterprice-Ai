# Handoff: qa_security, wave 2
Branch: agent/qa_security/w2   Base: main @ 929dcf3   Contract: contracts/api/CONTRACT.md + backend/nexora/contracts.py (W2)

QA/Security never edits application code. Everything below lives in owned paths: backend/tests/{isolation,security,
ai_eval,integration}/**, backend/testing/**, testdata/**, e2e/**.

## Done
- **A. Wave-1 follow-ups** (cce71a8, 26982bb)
  - `test_tenant_unique_constraints_include_org`: explicit, documented allowlist of GLOBAL pre-tenant lookup tokens
    (`accounts.ApiKey.prefix`, `orgs.Invitation.token_hash`). Any other unique field without org fails. There are
    guard tests to check that the allowlist is exact and still needed, and that it does not hide other fields.
  - The 6 W1 core-defect repros are no longer strict-xfail. They are plain tests that fail until the Backend W2
    fixes merge. Their docstrings say "Regression for QA defect ...". The defects are core-tenancy-guc-leak,
    core-tenanttask-actor-leak, core-storage-traversal, core-storage-content-disposition and
    core-finalize-rollback-outer-txn (plus the GUC transaction-local repro).
  - Endpoint matrix: anonymous requests now get 401 and authenticated non-members get 404. IDOR, list-leak and
    CSRF checks are unchanged.
  - `MissingIsolationFixture` now gives a precise message: the route name, the pattern, the view, the methods, the
    owning app, the exact file to edit and a copy-paste example. The registry checks for conflicting duplicate
    registrations across apps and for stale registrations (`stale_registrations`). There is also W2
    contract-route coverage for bi conversations/messages/feedback/exports/queries, data-sources (+test/introspect/
    import), pipelines, pipeline-runs, semantic and documents (+ask). An unrouted path is reported as a skip that
    names the owner.
- **B. SQL guard** (1503977): `backend/tests/security/sql/corpus.yaml` now has **163 cases**. The expected codes
  are exactly `nexora.contracts.GUARD_REJECT_CODES`, and the loader rejects unknown codes. New cases cover IN ()
  deny-all predicate rendering, mask bypass (row_to_json, `t.*`, ORDER BY / GROUP BY on a masked column), a CTE
  shadowing a logical table, set-op smuggling, unicode-escaped identifiers, and prompt-injection text in a cell
  value. An independent sqlglot oracle checks masks and deny-all. A Hypothesis differential test checks that
  anything `validate()` accepts re-parses and references only policy tables. Everything skips cleanly while
  `apps.sqlguard.services.validate` is absent.
- **C. Integration + golden set** (543c867, 8407499)
  - `backend/tests/integration/test_e2e_bi_flow.py` is DB-backed and uses only public APIs/services. It runs
    signup → org → upload `testdata/generated/sales.csv` → ingest (Celery eager) → profile + quality →
    `dataset_semantics` → POST bi conversation → "total revenue by region" (deterministic builder, no Gemini).
    It then checks that the SSE/JSON events carry sql, a result with the 5 regions (+ the NULL group), a chart and
    insights, that a BiQueryAudit row exists, and that a second org gets 404 for the conversation, dataset and
    audit. Each step that is not merged yet skips with a clear reason.
  - `backend/tests/ai_eval/golden_sales.yaml` has **48 questions** over sales.csv. Each has expected result checks
    computed by an independent pandas oracle (not exact SQL). The runner evaluates the deterministic builder, plus
    recorded Gemini cassettes when present (never live).
  - **Builder-only accuracy: 43/48 = 89.6%, 100% precision when answered.** The builder declines g39 (weekly
    orders last month) and g45 to g48 (they need the LLM path; no cassettes are recorded yet, so they skip).
  - Supporting helpers: gated Gemini fakes (payload gate), memory storage, SSE helpers, upload flows aligned to
    as-built error codes.
- **D. W2 security tests** (cc3ed8a), which skip cleanly while endpoints are absent:
  - Connector SSRF: private, loopback, link-local, IPv6 (incl. mapped/ULA) and metadata hosts, plus DNS rebinding
    (resolve-once pinning).
  - Credentials are never returned or logged (caplog canaries).
  - Pipeline DAG injection: expression strings, path traversal in names, cross-org dataset ids. Positive controls
    show that a rejection comes from the check.
  - Export CSV/XLSX formula injection.
  - Hostile document uploads: zip-bomb docx, wrong magic, oversized.
  - Document Q&A never sends real-document text to Gemini under synthetic_only (fake Gemini + payload gate).
- **E. e2e** (10830f9): Playwright selectors now match the merged frontend in `frontend/src` on main.
  - Signup uses the exact field labels and "Create workspace", then lands on `/o/<slug>/dashboard`, where it
    checks "Synthetic only".
  - It opens the sidebar `navigation "Main"` → Datasets (`/o/<slug>/data/datasets`) → the "Upload dataset" dialog:
    Dropzone `dropzone-input`, Dataset name, Classification combobox → "Synthetic", attestation checkbox, then
    "Upload and process".
  - It waits for the live job stream to finish ("Ingested, profiled and quality-scored."), then "Open dataset".
    Detail must show a Ready status, 5,020 rows, preview rows and a schema.
  - It then opens sidebar "Ask AI". On main, `/ask` is still the honest PlannedModule placeholder, so the question
    step is recorded as a `not-exercised` annotation and never faked. When the real page lands, the test asks
    "total revenue by region" and expects SQL plus the 5 regions.
  - A cross-org test uses a second user in a fresh browser context. It checks the UI shows "Organization not
    found", that a non-member gets 404 and an anonymous caller gets 401 from the API, and that an unknown dataset
    id shows "Dataset not found".
  - a11y now covers the real routes (dashboard, data/datasets, ask, settings/ai-policy, settings) and dark/light
    themes via the account menu.
  - `E2E_SKIP_IF_DOWN` behaviour is kept.
- Housekeeping (1e69dd3): ruff format on owned dirs. The DAG payload now uses `TENANT_GUC` (check_conventions).

## Tests (REAL results, 2026-11-20; Postgres :5433 / Redis :6380 / MinIO :9000 were all unreachable)
Command: `pytest tests/isolation tests/security tests/ai_eval tests/integration` (junit totals)
- **937 collected: 154 passed, 4 failed, 519 errors, 260 skipped** (skipped includes 1 xfail).
  - **519 errors: all are DB setup errors** (`OperationalError: connection timeout expired`, localhost:5433). They
    were **not executed because infra was unavailable**. This covers the matrix, RLS, tenancy core, ai_policy,
    secrets-in-logs, uploads, audit append-only, migration ops and the e2e BI flow.
  - 4 failed:
    - `test_guc_is_transaction_local_on_a_reused_connection` failed on the same DB connection timeout, so it was
      not really executed.
    - `test_storage_keys::test_check_key_rejects_path_traversal_segments` and
      `::test_presigned_filename_is_sanitised` are **intended** failures: W1 defects, fixed on the Backend W2
      branch, which is not merged here.
    - `test_endpoint_matrix::test_every_resource_route_has_an_isolation_factory` is a **real gap**: no app has
      added `apps/<app>/tests/isolation.py` yet (see defects).
  - 260 skipped: W2 endpoints not merged (pipelines / documents / data-sources / exports / bi), `sqlguard.validate`
    absent, and golden g45 to g48 with no cassettes.
- Golden set (builder only, no DB): 43/48 = 89.6%.
- e2e: `tsc --noEmit` OK. `E2E_SKIP_IF_DOWN=1 npx playwright test` → 10 skipped (stack down: /healthz unreachable).
- Checks: check_conventions OK, check_ownership OK, ruff check OK (owned dirs), manage.py check: 0 issues,
  makemigrations --check: no changes.

## Requests
- Integrator: run the owned suites again with infra up after merging Backend W2 (core fixes) and the W2 feature
  branches. The 519 DB tests have not been executed in this wave.
- AI/ML: record Gemini cassettes for golden g45 to g48 (and any LLM-path questions) so the runner can score the
  LLM path offline.
- Frontend (W3): the Ask AI page should expose a labelled question textbox (name matching /ask|question|message/)
  and render the SQL and the result table. The smoke test then drives it automatically.

## Open issues
- Nothing DB-backed ran this wave (infra down). Those results are unknown, not passing.
- The Ask AI e2e step is not exercised while `/ask` is a Planned placeholder.

## Defects for other agents
- **All app owners (orgs, accounts, governance, datasets, quality, jobs/audit as applicable; W2 apps too):** tenant
  routes with resource kwargs have no isolation factory. Failing test:
  `backend/tests/isolation/test_endpoint_matrix.py::test_every_resource_route_has_an_isolation_factory`
  (backend/tests/isolation/test_endpoint_matrix.py:77). Missing:
  - orgs: member-detail, invitation-detail
  - accounts: api-key-detail
  - governance: policy-preview, user-attributes, dataset-acl-list/detail, column-policy-list/detail,
    row-policy-list/detail
  - datasets: upload-detail, dataset-detail, dataset-classification, dataset-columns, dataset-preview,
    dataset-versions
  - quality: dataset-quality, dataset-quality-evaluate, dataset-quality-rules, dataset-quality-rule

  Each owner adds `backend/apps/<app>/tests/isolation.py` with `ISOLATION_FACTORIES` / `KWARG_FACTORIES` (the
  failure message contains a copy-paste example). Without these, the IDOR matrix probes with random ids only.
- **Backend (core), still open until the W2 branch merges:**
  - core-storage-traversal: `backend/tests/security/test_storage_keys.py:85`
  - core-storage-content-disposition: `backend/tests/security/test_storage_keys.py:131`
  - core-tenancy-guc-leak: `backend/tests/isolation/test_tenancy_core.py:117`
  - core-tenanttask-actor-leak: `backend/tests/isolation/test_tenancy_core.py:282`
  - core-finalize-rollback-outer-txn: `backend/tests/isolation/test_endpoint_matrix.py:231`
