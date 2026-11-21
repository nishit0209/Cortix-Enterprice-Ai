# W5 handoff: ai_ml

## Done
- **ML promotion via approvals** (CONTRACT W5). `POST ml/models/{id}/versions/{v}/promote`:
  - `staging`/`archived` are immediate (200).
  - `production` calls `apps.automation.services.request_approval(kind="model_promotion", approver_permission="ml.promote", impact="high", payload={model_id, model_name, version, stage, algorithm, primary_metric, primary_value, current_production, hook:"apps.ml.services.apply_promotion"})` and returns **202** `{status:"pending_approval", approval_id, model_id, version, stage, model_version}`.
  - The hook `apps.ml.services.apply_promotion(approval)` promotes on `status` "approved" (the old production version is archived), is a no-op for other statuses, and is audited with `approval_id`. It reads `payload`, `status`, `decided_by_id`/`decided_by` and `requested_by_id`/`requested_by`, and must be called inside the org's tenant context.
  - While `apps.automation.services.request_approval` is missing, or when `ML_PRODUCTION_REQUIRES_APPROVAL=False` (setting, default True), production promotion stays immediate (200).
- **Automation steps** are registered in `AppConfig.ready()` through `apps.automation.registry.register_step` (a no-op while the registry is missing):
  - `ml.train` `{project_id, algorithms?, random_state?, register?=true, model_name?, promote_to?=none|staging|production}`;
  - `ml.batch_predict` `{model_id, dataset_id, version?, output_name?, output_dataset_id?, explain?}`;
  - `documents.process` `{document_id | document_ids[≤20]}`.
  - Handlers run synchronously (inline training, scoring and processing) and return JSON.
  - They re-check the actor's permissions (`ml.manage`; `ml.promote` for promote_to; `documents.manage`) via `members_with_permission`. The actor comes from `ctx.actor_id`/`user_id`/`actor.pk`/`user.pk`, or the current actor ContextVar.
- **Event `document.processed`** is emitted after successful processing through `apps.automation.events.emit` (lazy import). Payload is metadata only: id, title, doc_type, classification, collection_id, pages, chunks, entities, tables, language.
- **Customer 360 recipe** (`apps/ml/recipes.py`, re-exported from `apps.ml.services`):
  - `customer_features(customers, orders, horizon_days=180)` is point-in-time. Behaviour is computed up to `as_of` (last order minus horizon), and the target `clv_next_180d` is completed spend after it. The churn label is `churned`, and PII/id columns are never used as features.
  - `publish_customer_features`, `create_customer_projects` (churn = classification, CLV = regression; idempotent), `train_customer_projects`, and `customer360_recipe(org_id=…)`. The last one is called outside a tenant context.
  - CLI: `manage.py ml_customer_recipe --org <slug> --user <email> --customers <id|name> --orders <id|name> [--train] [--promote staging|production|none]` or `--features <id>`.
  - Verified on testdata (721 customers): CLV mean 12,798 (not churned) vs 345 (churned).
- **Documents, Phase 8** (all local by default):
  - `document-collections` GET/POST, `/{id}` GET/PATCH/DELETE. Documents keep `collection_id` on upload and PATCH, and lists filter with `?collection_id=`.
  - `POST documents/{id}/extract {schema_key: invoice|receipt|resume, mode?: local|ai}` returns 201 with an `ExtractionResult` (`needs_review`). Every non-null field has a citation `{page,start,end,quote}`, and uncited fields are null.
  - `mode=ai` needs `documents.ask` and Gemini (`docs.extract`) goes through the policy gate. Quotes are verified, and unverifiable values are dropped. When the AI is blocked or unavailable, the local result is returned with a `reason`.
  - `GET documents/{id}/extractions`, `GET document-extractions/{id}`, and `POST document-extractions/{id}/review {decision: approve|reject, corrections?, note?}`.
  - `POST documents/compare {a, b, summary?}` does a local section alignment with a word diff and lists entity changes (amount/date/org/gstin). `summary=true` needs `documents.ask` and uses `docs.compare` through the policy gate.
  - `GET document-tables/{id}` and `POST document-tables/{id}/promote {name?, classification?, attest?}` (`datasets.create` + `documents.view`) call `publish_frame` with source `("document", id)`, which records lineage document → dataset. The response is 201 when ready, otherwise 202, and `promoted_dataset_id` is set on the table.
  - New models `DocumentCollection` and `ExtractionResult` (RLS), plus `Document.collection` and `DocumentTable.promoted_dataset_id`, in migration `documents/0002`. Isolation factories cover every new route.
- **Fixes:**
  - `PredictionLog.input_sha256` renamed to `input_hash` (DATABASE_SCHEMA name; migration `ml/0002`). This makes `test_prediction_log_stores_no_raw_rows` pass.
  - `test_permissions_and_cross_org` now matches the W4 grants (Analyst has `ml.manage`, Viewer has `ml.view`).
  - `ai/timeutil` accepts a `date`.
- **AI:** new prompts `docs.extract`, `docs.compare`, and `products.enrich` (VALUES, policy-gated; requested by data_engineering in W4).

## Tests (real PostgreSQL, PYTEST_DB_SUFFIX=ai_ml)
- apps/ai + apps/nl2sql + apps/documents + apps/ml (serial run): **974 passed, 0 failed, 0 errors, 0 skipped**.
- New tests:
  - `apps/ml/tests/test_w5_automation_db.py` (6): approval flow, steps, permission re-check, registry.
  - `apps/ml/tests/test_w5_customer_recipe.py` (3).
  - `apps/documents/tests/test_w5_intel_db.py` (8).
  - `apps/ai/tests/test_w5_prompts.py` (3).
- `tests/isolation` (all apps): all passed. `tests/security/test_ml_security.py`: everything passes except the test defect below. It passes when that one line is fixed locally.
- Checks: check_conventions OK, check_ownership OK, ruff clean, `manage.py check` OK, `makemigrations --check` shows no changes.

## Requests / defects for others
- **qa_security:**
  - `tests/security/test_ml_security.py::test_no_ml_route_accepts_an_uploaded_pickle` filters on `route.pattern.lstrip("^").startswith("ml/")`, but patterns are `api/v1/orgs/<slug:org>/^ml/...`, so nothing is probed. Use `route.pattern.split("<slug:org>/", 1)[-1].lstrip("^").startswith("ml/")`.
  - `_trained()` in that file asserts 200 for a production promote. Once automation ships, that is 202 (CONTRACT W5). Set `settings.ML_PRODUCTION_REQUIRES_APPROVAL = False` in the `ml_env` fixture, or promote to `staging` (serving uses production or staging).
- **backend (automation):**
  - When an approval of kind `model_promotion` is decided, call `payload["hook"]` (`apps.ml.services.apply_promotion(approval)`) inside the org's tenant context. The approval needs `.id`, `.status` ("approved"), `.payload`, `.decided_by_id`, `.requested_by_id`.
  - Step handlers expect `ctx.actor_id` (or `ctx.user_id`).
- **data_engineering / test infra:**
  - `apps/datasets/tests/helpers._drop_test_warehouse_objects` (session-scoped autouse) drops every `t_<suffix>_*` warehouse schema. Under `pytest -n` the workers share that prefix, so the first worker to finish drops the other workers' tables ("relation … does not exist"). Make `WH_SCHEMA_PREFIX` include the xdist worker id, or run DB suites serially.
  - C360 scoring: build the scoring frame with `apps.ml.services.customer_features(customers, orders)` so it carries the churn and CLV model features, then call `predict_frame`.
- **frontend:**
  - Promote can return 202 `{status:"pending_approval", approval_id}`.
  - New document routes are listed above. Extraction results have `result`, `field_citations`, `field_confidence`, `fields_found`/`fields_total`, `method`, `reason`. Compare returns `sections[{status, title_a/b, path_a/b, pages_a/b, similarity, changes[{op,a,b}]}]`, `entity_changes`, `counts`, `similarity`, `summary`, `reason`.

## Open issues
- None in owned code.
