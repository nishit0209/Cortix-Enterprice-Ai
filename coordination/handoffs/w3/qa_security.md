# Handoff: qa_security, wave 3
Branch: agent/qa_security/w3   Base: main @ ce5969a   Contract: contracts/api/CONTRACT.md (Local runtime, W2 as-built, W3 additions)

QA/Security never edits application code. Everything below lives in owned paths: backend/tests/{isolation,security,
ai_eval,integration}/**, backend/testing/**, testdata/**, e2e/**.

## Done
- **A. SQL guard oracle** (3d776da)
  - `backend/testing/sqlguard.py`: `mask_null` is accepted in exactly the approved rendering
    `CASE WHEN FALSE THEN col END` (one WHEN, condition the literal FALSE, the column as THEN, no ELSE, no
    operand), or as a NULL that does not mention the column. The exemption is per column reference, so a raw
    sibling reference is still flagged in the projection, WHERE, GROUP BY, ORDER BY, HAVING or a JOIN. It applies only
    to columns whose mask is `mask_null`. Every other check is unchanged and strict.
  - New oracle self-tests: 4 accepted renderings; 12 rejected shapes (`WHEN TRUE`, `WHEN 1 = 0`, `WHEN NOT TRUE`,
    an ELSE, a second WHEN, a simple CASE with an operand, COALESCE/concat with a raw column, the bare column,
    `upper(col)`, and so on); a per-clause exemption test; and a test that the wrapper does not excuse other mask kinds.
  - **`tests/security/sql` against the guard on main: 208 passed, 2 failed, 2 skipped. It is NOT fully green.**
    This branch changes no application code, so the guard under test is main's.
    - The 2 failures are mask-002 and mask-008 (policy `masked_multi`, check `contains_ci: sha256`). They are a
      **real guard deviation, not an oracle or corpus mistake**:
      - `mask_hash` is rendered `MD5(CAST(col AS TEXT) || %(salt)s)`;
      - SECURITY_ARCHITECTURE §5 specifies `encode(sha256(convert_to(col::text || <org salt param>,'UTF8')),'hex')`;
      - the CONTRACT ("Guard masks") approves only the `mask_null` rendering.

      The check stays strict (see Defects). The masked value is never raw: the oracle's raw-column checks pass for
      both cases, and only the hash function deviates.
    - The 2 skips are `stage: execute` cases. They are decided by EXPLAIN in the warehouse suite and need a database.
    - The 7 former `mask_null` failures now pass.
  - **The W2 reject-code name mismatches are gone on main.**
    - Neither `forbidden_construct` nor `system_catalog` remains anywhere in the corpus.
    - The corpus has 163 cases, 117 of them `expect: reject`, and uses only `nexora.contracts.GUARD_REJECT_CODES`.
      The loader asserts this.
    - Every reject case passes with a contract code.
- **B. Local-runtime contract tests.** Each one skips cleanly while its symbol is missing (6e665e0).
  - `tests/isolation/test_local_task_runner.py` (transactional DB) covers `TASK_RUNNER=thread`:
    - the run happens on a pool thread (not inline), with the tenant GUC, the org ContextVar, the actor and RLS
      (only the task's org rows) inside;
    - nothing leaks into 2×`TASK_RUNNER_THREADS` later `NexoraTask` runs (GUC, org and actor are checked);
    - the task's backend connection disappears from `pg_stat_activity` after the run;
    - `self.retry(countdown=)` re-submits (attempts 0, 1, 2, then none);
    - a failing body marks the job `failed`, and the other org cannot see it;
    - `eager` still runs inline.
  - `tests/isolation/test_job_events_db.py` (transactional DB) covers `JOB_EVENTS_BACKEND=db`:
    - the table has RLS enabled and forced, plus the tenant policy, and is marked append-only;
    - seq is increasing and `publish()` returns it;
    - an org-A publish for org B's job id never lands in B's stream;
    - an UPDATE is refused;
    - SSE replays every row with `id == seq`, ends after `done`, and `Last-Event-ID` skips delivered events;
    - SSE isolation: a non-member gets 404 (through both slugs), anonymous gets 401, and no A data appears in B's stream.
  - `tests/security/test_fs_storage.py` (no DB) covers `STORAGE_BACKEND=fs`, detected by behaviour: the S3 client is
    forbidden and the object must land at `LOCAL_STORAGE_DIR/<key>`.
    - Round-trip; 11 traversal/foreign/absolute/NUL/CRLF keys × 5 operations rejected without touching any file.
    - Windows-special names (`C:`, ADS `:stream`, `CON`, `nul.txt`, trailing dot/space, `|?"<>`) are rejected or stay inside the root; symlink escape.
    - Org A cannot get, presign, delete, open or check existence of org B's key.
    - No torn reads under concurrent overwrite; overwrite while a download holds the file open.
    - Signed URL: 200 streams the bytes; **expired → 403** and **6 tamper variants → 403** (payload swap to another
      org's key included); a deleted object → 403/404 with no bytes.
    - `Content-Disposition` is sanitised for CRLF/quote injection, `../`, `..\`, NUL and Unicode names.
  - `tests/integration/test_local_runtime_ops.py` covers the scheduler and health:
    - exactly one of two concurrent ticks runs a due job;
    - no tick runs while another session holds `pg_try_advisory_lock(ADVISORY_LOCK_KEY)`, and it runs once the lock is released;
    - no scheduler thread runs under pytest;
    - `/readyz` without Redis gives `redis: not_configured`, `storage: fs`, 200;
    - with the DatabaseCache table missing it gives 503 plus "createcachetable";
    - `/healthz` is 200 without Redis.
  - Fixture hygiene: `fake_redis` now pins `JOB_EVENTS_BACKEND="redis"`, and the recording-S3 fixture in
    `test_storage_keys.py` pins `STORAGE_BACKEND="s3"`. Without this, the existing redis/S3 tests would exercise
    the wrong backend once the test settings default to `db` and `fs`.
- **C. `seed_demo` integration test** (`tests/integration/test_seed_demo.py`, transactional DB, 53df45a).
  - It runs `seed_demo --email --password --org-name|--org`; the option name is introspected from the parser.
  - Checks after the run:
    - the login and the org slug are printed; there is one org, and its policy is `synthetic_only`;
    - sales, customers, products, orders and support_tickets are datasets in status `ready`, classified `synthetic`,
      with the exact CSV row counts, a `quality_score` in (0, 100], profiled columns and a quality report;
    - the `/data-stats` KPIs (datasets, rows_total, avg_quality, storage_bytes) are non-zero;
    - exactly one default dashboard has 4–6 widgets, and every data widget returns rows with `refreshed_at`;
    - the revenue-by-region widget and the total-revenue KPI equal the independent oracle (`testing.golden`);
    - a builder question ("total revenue by region") answers correctly, with no `nl2sql.generate` prompt, no blocked
      payload and no external network.
  - **Idempotency:** a second run leaves datasets with their row counts, versions per dataset, dashboards, widgets,
    conversations, users and orgs unchanged.
- **D. Isolation matrix (W3)** (53df45a).
  - `test_endpoint_matrix.py`: the W3 contract routes join the coverage list: saved-queries, dashboards, widgets,
    widget data, notifications, read, read-all, and ai/status. Once routed, they get the cross-org 404/401, IDOR,
    list-leak and CSRF probes automatically, and they must have isolation factories.
  - The coverage list now follows the as-built CONTRACT routes (11749f5).
    - It held API_SPECIFICATION's org-level `/semantic-models[/{id}]` and `GET /exports/{id}`, which were never
      built, so those entries could only ever skip. The CONTRACT's W2 as-built shape is semantic model + metrics per
      dataset, and `POST /exports` streams the file.
    - The list now asserts `datasets/{id}/semantic-model`, `/refresh`, `/metrics` and `/metrics/{id}`. All 4 pass:
      each is a discovered tenant route with an isolation factory.
  - `tests/isolation/test_w3_isolation.py`:
    - **inside one org**, notifications are private: user B cannot list user A's notifications, mark them read
      (404) or clear them through `read-all`;
    - `notify()` needs a tenant context;
    - notifications never cross orgs;
    - **inside one org**, a BI conversation is private to its author: analyst and viewer get 404 on detail,
      messages, posting and feedback, and the list does not show it;
    - org A's dashboards and widgets are unreachable from org B: GET, PATCH and DELETE on detail, widgets, widget
      and data through both slugs, with no content leak and no change on A;
    - a widget cannot bind org A's `saved_query_id` / `message_id` to a B dashboard;
    - a viewer can view but not modify a dashboard (403); anonymous gets 401.
  - `tests/integration/test_dashboard_widget_policy.py` takes ONE widget, pinned from an Ask-AI answer:
    - the owner sees the oracle result; the analyst without a policy sees the same rows;
    - **row policy** region = North gives exactly `[North]` with the owner's North value;
    - **column deny** on revenue: no revenue value leaves the guard (it rejects with `column_not_allowed`, or the
      column is absent);
    - **mask_null** on region: a single NULL group carrying the full total, and no region names in the response;
    - the owner's result is unchanged afterwards, and refreshes are audited with source `dashboard` (≥ 5);
    - a non-member and the other org get 404 for widget data.
- **E. testdata** (96ce339). `testdata/generate.py` adds a `commerce` generator. Regenerated with
  `--anchor 2026-11-17`: every existing file is **byte-identical**, so the golden set and the e2e numbers are
  unchanged. New files:
  - `customers.csv` (800): the sales customers C00001..C00800 with the same names (reproduced from the sales
    stream), plus segment, signup, tier, opt-in and a `churned` label.
  - `products.csv` (78): 58 canonical listings, including pack-size **variants** (different products), and
    20 **duplicate listings** of the same product under another SKU format (spaced / compact / `OLD-` legacy /
    unit conversion 5 l → 5000 ml), with an upper-cased, bracketed-brand or typo name.
  - `orders.csv` (8,723): order lines driven by a latent per-customer value and frequency (**CLV-like**: the
    top 20% of customers hold > 45% of revenue; Enterprise > SMB > Consumer) and a monthly **churn hazard**
    (frequency drops 2 months before a churn and stops after it). Duplicate listings are sold too.
  - `support_tickets.csv` (1,534): volume rises and CSAT falls with dissatisfaction and before a churn
    (churned customers: 2.7 tickets vs 1.6 for active ones; CSAT 2.96 vs 3.53).
  - `commerce_meta.json`: the hidden truth (churn dates, latent satisfaction/rate/value, duplicate pairs,
    variant families).
  - `tests/integration/test_testdata.py` has 6 new property tests: manifest, referential integrity and amounts,
    same names as sales, a learnable churn signal, CLV heterogeneity, and ER duplicates vs variants.
    README updated.
- **F. e2e** (a0bcdc0).
  - Local-run config: `baseURL` = `E2E_BASE_URL` or `http://localhost:3000` (Next.js proxies `/api` and
    `/healthz`). `E2E_SKIP_IF_DOWN=1` is kept. README updated for runserver + `npm run dev`.
  - The smoke test drives Ask AI through `getByRole("textbox", { name: "Ask a question", exact: true })` and
    asserts that the SQL (`select … from`) and a result **table** with the 5 regions as cells render.
  - New `tests/dashboards.spec.ts`:
    - **pin flow**: answer → "Pin to dashboard" → dialog (combobox "Dashboard" → "+ New dashboard…", "New
      dashboard name", "Widget title", "Pin") → the API shows 1 widget with re-executed data → the dashboard page
      renders the widget and the regions;
    - **bell**: the accessible name equals the API unread count (`Notifications, N unread`); "Mark all read"
      brings the API count to 0;
    - cross-org: dashboards and notifications return 404, and the UI shows "Organization not found".
  - The a11y suite adds `/o/<slug>/dashboards`.
  - Steps whose UI is not merged are recorded as `not-exercised` annotations, never faked.
- **G. Golden runner** (fdab1a2). Cassettes are read from env `NEXORA_CASSETTES_DIR` first (also
  `--cassettes-dir`), then from the committed `backend/tests/ai_eval/cassettes/`.
  - **Format for AI/ML's `record_cassettes --out <dir>`:** `<dir>/<golden id>.json` or `<dir>/nl2sql/<id>.json`,
    containing
    `{"format": "nexora.cassette.v1"?, "id": "g45"?, "question": "<golden question>"?, "prompt_key": "nl2sql.generate"?, "model"?, "recorded_at"?, "response": {<generate_structured dict: sql, explanation, assumptions, chart_hint, clarification>}}`.
    `response` is required; the other fields are optional, but when present they must match.
  - A malformed or stale cassette raises `CassetteError`; it is a recording defect, never a skip. The report
    counts `cassette_answered` / `cassette_correct` separately.
  - **Builder-only accuracy on main: 43/48 = 89.6%.** It answered 43, all correct, so precision when answered
    is 100%. This was measured with `python -m testing.golden` at HEAD, which has no application changes vs main.
    - The builder declines g39, which is a strict xfail, and g45–g48, which skip until cassettes exist.
    - No cassette is committed and `NEXORA_CASSETTES_DIR` is unset. `--with-cassettes` therefore gives the same
      43/48, with 0 cassettes answered.

- **Test hygiene** (b67e8d8): `tests/isolation/test_tenancy_core.py::test_guc_is_transaction_local_on_a_reused_connection`
  now requests `django_db_setup`.
  - It opens a raw psycopg connection from the Django settings and requested no DB fixture.
  - Run alone, it would have targeted the configured (non-test) database.
  - With no database it was the one owned test reported FAILED. It now errors at setup like every other DB test.

## Tests (REAL results at HEAD, 2026-11-20; no database: Postgres on :5432 rejects `nexora_migrator`, i.e. not bootstrapped)
Command (from backend/): `pytest tests/isolation tests/security tests/ai_eval tests/integration -p no:cacheprovider`

**Total: 380 passed, 2 failed, 127 skipped, 1 xfailed, 1044 errors** in 34 s.

| suite | passed | failed | skipped | xfailed | errors (no DB) |
|---|---|---|---|---|---|
| tests/security/sql | 208 | 2 | 2 | 0 | 0 |
| tests/security (other) | 36 | 0 | 85 | 0 | 118 |
| tests/isolation | 47 | 0 | 26 | 0 | 911 |
| tests/integration | 13 | 0 | 10 | 0 | 15 |
| tests/ai_eval | 76 | 0 | 4 | 1 | 0 |

- **Failures:** the 2 failures are mask-002 and mask-008, the guard defect for data_engineering.
- **Errors:**
  - All 1044 errors happen at setup, with the same `OperationalError: password authentication failed for user
    "nexora_migrator"`. I checked the log error by error.
  - They were **not executed: no database**. That includes all the new W3 DB tests: runner, job events,
    scheduler/readyz, seed_demo, W3 isolation and widget policy.
  - The single warning is Django's no-database "postgres database" RuntimeWarning.
- **Skips (127):**
  - 121 wait for Backend W3 symbols that are not on main: fs storage 85, scheduler/readyz 7, job events 6, thread
    runner 6, W3 isolation 6, widget policy 2, seed_demo 1, and 8 W3 routes in the matrix coverage list.
  - 2 are SQL `stage: execute` cases.
  - 4 are golden g45–g48, which wait for AI/ML cassettes.
- **xfail:** golden g39 (builder gap, strict).
- **Sanity check against the Backend W3 tree.** I ran the fs-storage test file read-only against the Backend W3
  tree, using a copy of the test file with that tree's code, to check the test itself.
  - The traversal, isolation, signature-validity and Content-Disposition tests pass there.
  - The 403-vs-404/410 status codes and the Windows overwrite case fail.
  - Re-checked by reading the code at the backend branch head (55db137); both behaviours are unchanged. See Defects.
- **Golden:** builder 43/48 = 89.6%.
- **e2e:** `tsc --noEmit` OK. `E2E_SKIP_IF_DOWN=1 npx playwright test` gives 13 skipped, because the stack is down.
- **Checks:**
  - `check_conventions` OK; `check_ownership --agent qa_security --base main` OK;
  - ruff check and ruff format OK on the owned dirs (55 files);
  - `manage.py check` reports 0 issues;
  - `makemigrations --check` reports no changes. Its history-consistency check was skipped: no database.

## Requests
- **Integrator:**
  - Decide the status codes of `/api/v1/files/<token>`. The QA and Frontend W3 missions say **403** for both
    expired and tampered links. The Backend W3 work in progress returns 404 for a tampered link and 410
    (`download_expired`) for an expired one. My tests assert 403. Please put the decision in the CONTRACT.
  - Add the cassette format above to the CONTRACT, or point AI/ML at this handoff.
  - Choose one fix for `mask_hash`:
    - Data Engineering renders the SECURITY_ARCHITECTURE §5 sha256 form (see Defects). This is QA's
      recommendation.
    - Or approve the MD5 rendering in CONTRACT "Guard masks", as was done for `mask_null`. QA then relaxes
      mask-002 and mask-008 to that exact rendering.

    Until then, the suite stays red on purpose.
  - Reconcile `docs/API_SPECIFICATION.md` with the as-built CONTRACT routes. It still lists
    `/orgs/{org}/semantic-models[/{id}[/generate|/publish|/metrics]]` and `GET /orgs/{org}/exports/{id}[/download]`.
    What was built is `datasets/{id}/semantic-model` (+ `/refresh`), `datasets/{id}/metrics[/{id}]`, and a
    streaming `POST /exports`.
  - When the DB exists, run the owned suites, including the transactional ones (`databases="__all__"`).
- **AI/ML:** `record_cassettes --out <dir>` should write `<dir>/<id>.json` in the format above. Include `id` and
  `question`, so stale recordings are detected.
- **Backend:** `seed_demo` dataset names should contain the CSV stem (e.g. "Sales", "Support tickets"); the test
  matches on the normalised name. The default dashboard needs a KPI widget whose single value is the total revenue,
  and a revenue-by-region widget.
- **Frontend:** the e2e uses these accessible names:
  - the "Pin to dashboard" button on an answer;
  - dialog "Pin to dashboard", combobox "Dashboard", option "+ New dashboard…", fields "New dashboard name" and
    "Widget title", button "Pin";
  - a link with the dashboard title on `/o/<slug>/dashboards`;
  - the bell `Notifications` / `Notifications, N unread`, the list "Notifications", items "Unread: <title>", and
    "Mark all read".

## Open issues
- Nothing DB-backed has ever run, so those results are unknown, not passing.
- Local-runtime / dashboards / notifications / seed_demo tests are contract-driven and unverified until Backend W3
  merges (and the DB exists).
- The e2e pin and bell flows have not been executed (no stack).
- `tests/security/sql` stays red (mask-002, mask-008) until the `mask_hash` rendering is fixed or approved.
- The fs-storage status-code tests (403) depend on the Integrator's decision about `/api/v1/files/<token>`.

## Defects for other agents
- **data_engineering:** `mask_hash` is rendered `MD5(CAST(col AS TEXT) || %(salt)s)`.
  - Where: `backend/apps/sqlguard/rewrite.py:45` (`_MASK_HASH`, used at :197-200), with `md5` the rewriter-only
    function in `backend/apps/sqlguard/allowlist.py:79`.
  - Spec: SECURITY_ARCHITECTURE §5 specifies `encode(sha256(convert_to(col::text || <org salt param>,'UTF8')),'hex')`.
  - Failing tests: `backend/tests/security/sql/test_sql_guard_corpus.py::test_guard_verdict[mask-002]` and
    `[mask-008]` (check `contains_ci: sha256`). On main: 2 failed of 212.
  - Fix: render the §5 form, and add `sha256`, `encode` and `convert_to` to `REWRITER_ONLY_FUNCTIONS`. Users still
    cannot call them: the `sha`, `encode` and `convert` prefixes stay forbidden for user SQL. The per-org salt is
    already HMAC-derived and bound as a parameter.
- **backend** (W3 branch, not merged; observed by running the test file there, and re-read at the branch head
  55db137; verify after merge):
  - `apps/core/files.py` returns 404 for a tampered token (:31/:37/:42) and 410 `download_expired` for an expired
    one (:35). The missions specify 403.
    - Failing tests: `backend/tests/security/test_fs_storage.py::test_expired_signed_url_is_403` and
      `::test_tampered_signed_url_is_403[*]`.
    - Pending the Integrator's decision above.
  - On Windows, the fs backend cannot overwrite an object while a download holds it open.
    - Cause: `os.replace` (`apps/core/storage.py:167`, in `_fs_write`) raises `PermissionError` [WinError 5], which
      surfaces as `StorageError`. No retry has been added since.
    - Failing test:
      `backend/tests/security/test_fs_storage.py::test_overwrite_succeeds_while_a_download_holds_the_file_open`.
    - A short retry on `PermissionError`, or a write to a fresh key, fixes it.
- **ai_ml:** golden g39 ("weekly orders last month") is still a builder gap (strict xfail in
  `backend/tests/ai_eval/test_golden_sales.py`). When the builder answers it, the xfail turns XPASS(strict); tell
  QA so the marker can be removed.
