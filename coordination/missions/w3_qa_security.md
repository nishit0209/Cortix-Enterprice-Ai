You own `backend/tests/{isolation,security,ai_eval,integration}/**`, `backend/testing/**`, `testdata/**`, `e2e/**` and `perf/**`. You NEVER edit application code. Report defects with file:line plus a failing test in `defects_for_other_agents`.

A. **SQL guard oracle.**
   - Accept the approved `mask_null` rendering `CASE WHEN FALSE THEN col END` (SECURITY_ARCHITECTURE §5 table, CONTRACT "W2 as-built additions"). It is safe: constant-false, type-preserving, folded by the planner.
   - Keep every other check strict.
   - Check against the guard on main that `tests/security/sql` is fully green. Investigate every remaining failure: a real guard bug is a defect for data_engineering; an oracle or corpus mistake you fix yourself.
   - The W2 Data Engineering handoff mentions reject-code name mismatches. Verify they are gone on main.

B. **Local-runtime tests** (CONTRACT "Local runtime"; the Backend agent builds it in parallel). Write contract-driven tests that skip cleanly while a symbol is missing:
   - TASK_RUNNER thread: tenant GUC and actor present inside the task, no leakage afterwards, connections closed, retry re-submits, failure marks the job failed.
   - JobEvent DB backend: publish/SSE replay with `Last-Event-ID`, cross-org isolation.
   - fs storage: traversal rejected; signed URL expiry and tamper → 403; filename Content-Disposition sanitized; another org's key cannot be read.
   - Scheduler: only one ticker under the advisory lock.
   - `/readyz` behaviour without Redis, and with a missing cache table.

C. **`seed_demo` integration test** (DB). After `manage.py seed_demo`:
   - the datasets are ready with profiles and quality;
   - data-stats KPIs are non-zero;
   - the default dashboard widgets return rows;
   - a BI builder question answers.

   Also check the command is idempotent: running it twice gives no duplicates.

D. **Isolation matrix.** Cover the new W3 routes: dashboards, widgets, widget data, notifications, files.
   - Test that within ONE org, user A cannot read user B's notifications or conversations.
   - Test that a dashboard widget's data under a restricted viewer (column deny / row policy) differs exactly as the policy specifies.

E. **testdata.** Make sure `testdata/generate.py` produces the CSVs `seed_demo` needs: sales plus customers, products, orders/transactions and support tickets. W5 Customer 360 / Product 360 and W4 ML will need them: include churn-like and CLV-like signals and product records with duplicate/variant SKUs for entity resolution. Keep everything deterministic and synthetic, and regenerate `testdata/generated/`.

F. **e2e** (Playwright).
   - Drive Ask AI through the labelled composer (`aria-label="Ask a question"`) and assert that the SQL and the result render.
   - Add a dashboard pin flow and a notifications bell check.
   - Add a local-run config: `baseURL` from env (default `http://localhost:3000`) with `E2E_SKIP_IF_DOWN`.

G. **Golden runner.** Read cassettes from a directory given by env `NEXORA_CASSETTES_DIR` (the format AI/ML's `record_cassettes --out` writes; coordinate through the contract and your handoff). Report builder-only accuracy on main.
