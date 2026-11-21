# W5 handoff: qa_security

## Done
- A. Fixed the test-side failures on main:
  - platform console: transactional + `ops` alias;
  - retention: ML telemetry tables are ops-purgeable; the 2 skipped tests now use the registered targets and run 30
    days ahead;
  - C360 precision/recall: `CustomerProfileLink`;
  - ML pickle probe route match and `PredictionLog.input_sha256`;
  - SSRF: a 400 `invalid` on `config.host` counts as a refusal.
- B. `tests/security/test_automation_security.py` (11 tests):
  - webhook HMAC: forged, other-org secret, missing signature or timestamp, tampered, stale, replay, replay with a
    refreshed timestamp, and session without a signature;
  - the secret is shown once and stored hashed; unknown and deleted triggers are refused;
  - approval re-auth for high impact, approver-only, cross-org 404, no double decision;
  - ML production promotion is 202 and promotes only after approval;
  - high-impact steps wait for approval;
  - steps run as the run's actor, with no escalation through a workflow (custom role with only `automation.*`);
  - the agent allowlist and injected-plan tools.
  - All shapes are in `backend/testing/automation.py`.
- C. `tests/integration/test_final_e2e.py`: seed_demo -> BI -> widget -> ML churn -> promote (approval) -> C360 score
  -> automation run.
- `testing.ml.promote_effective`: the ML suites approve 202 promotions (second admin + password).
- D. `backend/tests/FINAL_VERIFICATION.md`.

## Tests (real PostgreSQL, PYTEST_DB_SUFFIX=qa_security)
- Owned suites: 2310 passed, 1 failed, 22 skipped, 1 xfailed (16 min).
- After that run, the retention re-run passed 5/5, so the net result is 2312 passed, 1 failed, 20 skipped,
  1 xfailed.
- The 1 failure is the E2E stage 5 (DE defect 1 below). The 11 automation tests skip until Backend W5 is merged.
- conventions OK, ownership OK except the mission-placed FINAL_VERIFICATION.md (see Requests), ruff OK, manage.py check OK, makemigrations --check: no changes.

## Defects for other agents
1. **data_engineering:** apps/customers/services.py:933 `FEATURE_COLUMNS` / `features_frame()` has no label column.
   - The published "Customer 360 features" dataset therefore cannot train a customer churn model.
   - `POST customers/scoring-runs` with the org's churn model (trained on churn.csv) gives 400
     `prediction_input_invalid` (missing tenure_months, monthly_spend_inr, ...).
   - Fix: carry a mapped label (for example `churned` from the source attributes) into the features frame, or
     score with a documented customer project.
   - Fails `tests/integration/test_final_e2e.py`.
2. **data_engineering (W4 #4, still open):** apps/pipelines/steps.py:23 rename/derive `NewName` has no identifier
   pattern (xfail strict in test_pipeline_dag_injection).
3. **backend:** apps/governance/retention.py has no BEFORE TRUNCATE trigger request yet (W4 request, still open).

## Requests
- **Integrator:** after merging Backend W5, run `tests/security/test_automation_security.py`. If a shape differs,
  adapt `backend/testing/automation.py` only (never the assertions). In particular:
  - the webhook headers: signature in `X-Nexora-Signature` (or `X-Signature`), unix timestamp in
    `X-Nexora-Timestamp`;
  - the decision body `{decision: approve|reject, comment, password}`.
- **Integrator:** the mission puts `backend/tests/FINAL_VERIFICATION.md` at the tests root, which OWNERSHIP.md does
  not assign (check_ownership flags only this file). Please add it to qa_security's ownership, or move it.
