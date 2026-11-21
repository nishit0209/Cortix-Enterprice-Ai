# W5 fix — qa_security

Done (test side only):
- `backend/testing/automation.py`: workflows are POSTed in the as-built shape `{"name", "graph": {"steps": [{key, kind, config, depends_on}]}}` (the 422 was the notify config `message` field, rejected by the step schema); `request_approval` passes `requested_by` as the user UUID.
- `tests/security/test_automation_security.py`: notify configs use `title`/`body`; the customers.resolve owner positive control now only requires passing the permission check (a bare org has no customer sources, so the step legitimately stops there).
- `tests/integration/test_final_e2e.py`: maps the seeded `orders.csv` as an order event source, publishes the C360 training set (`{kind: "training"}`), trains churn on `churned` with `future_value`/`profile_id` excluded from features.
- `tests/security/test_pipeline_dag_injection.py[rename-to-sql]`: the app now refuses it (400, `apps/pipelines/steps.py:25` NewName validated); the test was wrong (stale strict xfail). Removed the mark. No defect for data_engineering.

Tests (real DB, suffix qa_security): automation security + pipeline DAG injection + final e2e: 33 passed, 1 skipped (agent step kind not registered), 1 deselected.
Open: `test_high_impact_step_never_runs_before_approval` still fails on main (high-impact step runs before approval) — kept as is, awaiting the backend fix.
Not mine: `apps/ml/tests/test_api_db.py::test_batch_predictions_publish_a_dataset` (409 model_not_ready) — ai_ml.
