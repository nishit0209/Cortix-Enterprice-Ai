# W5 fix - ai_ml

- Fixed `apps/ml/tests/test_api_db.py::test_batch_predictions_publish_a_dataset`: it promoted v1 to production, which since W5 requires an approval (409 model_not_ready). It now promotes to staging (asserts 200 + stage=staging); serving resolves production first, then the latest staging version, so batch scoring runs on the staging version and publishes the scored frame (240 rows, prediction + probability columns).
- Tests: apps/ml full suite on real Postgres: 47 passed, 0 failed.
- Checks: ownership OK, ruff OK, conventions OK.
- Requests / defects for others: none.
