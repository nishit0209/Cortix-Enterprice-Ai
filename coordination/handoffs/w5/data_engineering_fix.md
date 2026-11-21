# W5 fix — data_engineering

## Done
- Checked `tests/security/test_pipeline_dag_injection.py::test_untyped_or_raw_steps_never_run[rename-to-sql]` against the real DB. It fails as **XPASS(strict)**: the app now **rejects** the step with a 400. `NewName` in `apps/pipelines/steps.py` (commit 40c0de8) enforces `^[a-z_][a-z0-9_]{0,62}$`, and builder nodes are normalised (`dag._normalize_builder_nodes`) before the same `model.model_validate`. The app is correct. The stale `xfail(strict=True)` mark makes the test fail.
- Added regression tests in `apps/pipelines/tests/test_builder_dialect.py`. They check that unsafe rename and derive names are rejected in both the builder and canonical dialects, that raw and SQL-carrying steps (`sql.raw`, `python.exec`, `filter.sql`) are rejected, and that a safe rename is still accepted.

## Tests (real DB)
- `apps/pipelines`: 271 passed. `tests/security/test_pipeline_dag_injection.py`: all refusals hold, and the only failure is the XPASS(strict) above.
- ruff, check_ownership, check_conventions and manage.py check pass.

## Defects for others
- qa_security: remove the `pytest.mark.xfail(strict=True, ...)` from the `rename-to-sql` param in `backend/tests/security/test_pipeline_dag_injection.py`. The defect is fixed (NewName identifier pattern), so the param must now pass as a plain refusal.
