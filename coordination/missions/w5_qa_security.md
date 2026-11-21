Owned: backend/tests/{isolation,security,ai_eval,integration}/**, backend/testing/**, testdata/**, e2e/**, perf/**. Never edit app code.
A. Fix test-side failures on main:
   - tests/security/test_platform_console.py: read orgs outside a tenant context via the ops alias / Organization unscoped API; use
     django_db(transaction=True, databases="__all__");
   - tests/security/test_retention_usage.py (2 append-only tests);
   - tests/integration/test_customer_product_360.py precision/recall test (align with the as-built API, or report a DE defect with numbers).
   Report APP bugs as defects with file:line.
B. Automation security: webhook HMAC (forged, replayed, stale), approval re-auth for high impact, step permission re-check (no escalation),
   cross-org 404 on every new route, agents only call allowlisted tools.
C. A final end-to-end integration test on the real DB: seed_demo → BI question → dashboard widget → ML train churn → promote (approval) →
   C360 score → automation workflow run.
D. Write backend/tests/FINAL_VERIFICATION.md: what is covered and the REAL counts.
