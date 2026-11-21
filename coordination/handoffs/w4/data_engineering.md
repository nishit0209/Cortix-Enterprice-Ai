# W4 handoff: data_engineering

## Done
- **publish_frame** (CONTRACT W4): CSV via storage -> create_upload -> create_dataset/add_version; lineage from `source`.
- **governed_frame / policy_columns** (datasets): user-policy-filtered frames for ML / C360 / P360. Denied and masked columns are never loaded, and row policies apply.
- **W3 leftovers:** `data-stats` `*_prev` KPIs; real pipeline **append** mode (new version = previous rows + new rows); profiled `values` in `DatasetSemantics.columns_meta` for non-PII dimensions.
- **Customer 360** (`apps/customers`): sources (policy-checked mapping), identity resolution as a 202 job (normalize, block, rapidfuzz score, union-find with cannot-links, survivorship), review queue (confirm merges, reject splits/blocks; audited), profiles, timeline from event datasets, RFM quintile and rule segments, notify on finish, settings (thresholds/weights/survivorship per org). `POST customers/scoring-runs` returns 409 `ml_model_required` until a model exists (predict_frame is loaded lazily). `POST customers/features/publish` publishes non-PII features.
- **Product 360** (`apps/products`): sources, `POST products/normalize` (202): SKU/brand/unit/attribute normalization, duplicate/variant matching (SKU-scheme aware: two codes of one scheme are never merged or chained), masters with stable ids, attributes, quality score, review queue, corrections (applied ONLY on approve, kept across re-runs, audited), `POST products/enrich` (202, local rules -> proposed corrections), `POST products/search` (semantic via apps.ai embeddings + `nearest()`, text fallback with `search_mode`/`reason`). Routes: `products`, `/{id}`, `/{id}/attributes`, `settings`, `sources[/{id}]`, `normalize`, `enrich`, `runs[/{id}]`, `matches[/{id}[/decision]]`, `corrections[/{id}/decision]`, `search`.
- Fixes found by the DB runs: a malformed connector host is 400 (it was 422); GET pipeline schedule renders JSON `null`; uploads accept a JSON body, so non-members get 404 and requests without CSRF get 403 instead of 415.

## Entity-resolution evaluation (testdata/generated, ground truth from generate.py)
| Data | Precision | Recall | F1 |
|---|---|---|---|
| customers_crm + customers_web vs customers_ground_truth | 1.00 | 1.00 | 1.00 |
| products_supplier_a + b vs products_ground_truth | 0.92 | 0.88 | 0.90 |
| products.csv duplicate listings (commerce_meta) | 1.00 | 0.95 | 0.97 (0 variant pairs merged) |

The remaining product FPs/FNs are distinct masters with identical name, brand and size in the generator. Only price can tell them apart.

## Tests (real Postgres, PYTEST_DB_SUFFIX=data_engineering)
- Owned suites (connectors, datasets, pipelines, quality, lineage, warehouse, semantic, sqlguard, customers, products): **1092 passed, 0 failed, 0 errors, 0 skipped** (junit).
  - customers: 15 (9 unit + 6 API/DB); products: 30 (22 unit + 8 API/DB).
- `tests/isolation/test_endpoint_matrix.py -k "upload or customer or product"`: 271 passed (the new factories are in `apps/{customers,products}/tests/isolation.py`).
- Checks: check_conventions OK, check_ownership OK, ruff clean on owned dirs, manage.py check OK, makemigrations --check: no changes. No frontend files touched.

## Decisions
- Append mode is implemented for real; it is not refused.
- The product Gemini enrichment path is NOT offered: it needs a registered prompt (owned by AI/ML). Enrichment is local rules only and defaults to local.
- Product matching cap: two different SKUs of the same scheme score at most 0.70 (`DIFFERENT_SKU_CAP`) and are a cannot-link.

## Requests
- AI/ML: register a `products.enrich` prompt (VALUES payload, synthetic/public only under `synthetic_only`) if Gemini enrichment is wanted. Then P360 can add `mode: "ai"`.
- Frontend: P360 screens can use the routes above. `search` returns `search_mode` (semantic|text) and `reason`.

## Defects for others
- Backend (`apps/core/exceptions.py`): `response["Content-Type"] = "application/problem+json"` is overwritten by DRF's `Response.rendered_content`, so every error is served as `application/json`. Fix: set `response.content_type = "application/problem+json"`. This also fails `apps/accounts/tests/test_auth_api.py::test_signup_rejects_weak_password`.
- Isolation matrix failures outside my apps (seen in tests/isolation/test_endpoint_matrix.py): `bi-message-feedback-POST`, `document-detail-PATCH` and `document-classification-POST` validate the body before the tenancy 404 (they return 400); `ai-usage` list fails with a TypeError (`float()` of a `datetime.date`); `tests/isolation/test_jobs_sse_isolation.py::test_sse_access_check_matrix` fails.

## Open issues
- None in owned apps.
