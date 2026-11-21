You own `backend/tests/{isolation,security,ai_eval,integration}/**`, `backend/testing/**`, `testdata/**`, `e2e/**` and `perf/**`. You NEVER edit application code. Report defects with file:line plus a failing test.

## A. Wave-3 leftovers (these failures are on main now)

1. **Signed-URL status codes.** `tests/security/test_fs_storage.py` still expects 403. The CONTRACT ("W3 as-built additions": Signed file URLs) is now 404 for tampered and 410 `download_expired` for expired. Update the tests.
2. **Golden cassette tests.** `tests/ai_eval/test_golden_sales.py::test_cassettes_dir_env_is_searched_first_in_both_layouts`: the question it uses is now answered by the improved builder (source=`builder`). Use a question the builder declines, or force the cassette path.
3. **g39.** It now XPASSes strict. Remove `known_gap` from `golden_sales.yaml` g39 (and make g45 `builder: true` if the builder answers it). Report builder-only accuracy on main.
4. **Backend W3 requests:**
   - the `fake_redis` fixture must set `settings.JOB_EVENTS_BACKEND='redis'`;
   - the `tests/security/test_storage_keys.py` s3 fixture must set `settings.STORAGE_BACKEND='s3'`;
   - `test_jobs_sse_isolation`: a viewer may NOT follow another user's job (404); add an auditor holding `jobs.view_all`; drop the `pytest.warns` on the WSGI path.
5. **Dashboards.** Encode the as-built `is_shared` visibility rule (CONTRACT).

## B. ML security and correctness (AI/ML builds `apps/ml` this wave; contract-driven tests that skip cleanly until symbols and routes exist)

- **Artifact safety:** a tampered artifact (sha256 mismatch) is refused; artifacts are never loaded from outside the org prefix; no route accepts uploaded pickles.
- **Prediction API:**
  - an API key with scope `ml.predict` works only for its own org (cross-org 404);
  - a key without the scope gets 403;
  - more than 1000 rows gets 400;
  - a missing feature gets 400;
  - the PredictionLog stores no raw rows.
- **Policy:** features exclude denied and masked columns for a restricted user, so a model trained by a restricted user cannot use hidden columns.
- **Leakage guard:** ID-like and target-duplicate columns are dropped.
- **Metric oracle:** on a fixed synthetic dataset, the reported accuracy/F1/RMSE equal sklearn computed independently in the test.
- **Determinism:** the same `random_state` gives the same metrics.
- **Gemini:** never receives row values on the free tier (fake GeminiService + payload gate).

## C. Governance, usage and platform

- Non-platform users get 404 on every `/platform/*` route.
- Platform payloads contain no tenant data rows. Scan the responses for values from the synthetic testdata.
- A suspended org returns 403 `org_suspended` for its members, and reactivation restores access.
- Retention never deletes append-only tables, and dry-run changes nothing.
- Usage endpoints are tenant-scoped.

## D. Customer 360 / Product 360 (Data Engineering builds them this wave)

- Isolation matrix for every new route.
- An entity-resolution quality test against the `testdata` ground truth: precision ≥ 0.9 and recall ≥ 0.8 on customers; report product match precision/recall.
- Corrections apply ONLY after approve.
- Masked/denied columns cannot be mapped as customer source fields.

## E. e2e (Playwright, `E2E_SKIP_IF_DOWN`)

- ML flow: create project → train → see metrics → predict.
- Governance: create a column mask → policy preview shows it masked.
- Platform console gating.
