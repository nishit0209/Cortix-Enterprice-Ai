# W4 handoff: frontend

## Done
- **ML Workspace** `/o/[org]/ml`: projects list, new-project wizard (`/ml/new`: dataset, target + task, features with policy/leakage reasons), project tabs (EDA, recommendations, train with live job progress, experiments with 2–4-way compare and curves/confusion/residuals/importance/SHAP), model registry with promotion (confirm dialog, `ml.promote`), prediction playground (schema-driven form, SHAP top contributions), batch prediction to an output dataset, API usage panel (`ml.predict` key scope, no secrets), monitoring (PSI thresholds 0.1/0.25, prediction distribution).
- **Governance** `/o/[org]/governance`: dataset ACLs, column policies (allow/deny/mask), structured row-policy builder (no free SQL), user attributes, preview-as-member with an audited sample, retention policies (dry run, enforce), access history.
- **Org usage** `/o/[org]/settings/usage`: built from `GET /usage` and `/ai/usage`, with range presets or a custom range and a metric filter.
- **Platform console** `/platform`: shown only when `/me` has `user.is_platform_admin === true`. Everyone else gets the regular 404 page, and so does any platform route that answers 404/401/403. Tabs: orgs (search/status filter, detail, suspend/reactivate with confirm + audited reason), health (components + runtime), jobs (queue depth, by type, runner, failures/status filter), AI usage (per-model stacked chart, top orgs) + model-quota form (server bounds validated client-side), logs viewer (level/component filters, paging by `before_id`). The user menu links to it for platform admins.
- **W3 leftovers**:
  - `/data-stats` now uses `datasets_by_status`, `sources`, `runs_7d.total` and `runs_active`. KPI deltas come from the W4 `*_prev` fields (fallback: the legacy `previous` object).
  - Lineage uses the node's `restricted` flag and the server's "N restricted assets" label.
  - Document search shows `search_mode_message` on a full-text fallback, and documents show an "Embedding pending" badge from `embedding_status`.
  - The pipeline destination has an **Append** mode for existing datasets, because DE ships real append (38da914).
  - Import already sends `{schema, table}` separately. `download_url` and 410 `download_expired` were already handled.

## Tests (real)
- vitest: **27 files, 255 tests passed, 0 failed.**
- eslint: pass. tsc --noEmit: pass. next build: pass (`/platform` static, ML/governance/usage routes built).
- check_conventions OK; check_ownership OK (frontend); manage.py check: no issues; makemigrations --check: no changes.
- No backend code is owned by frontend, so no pytest suites were run.

## Requests
- None.

## Open issues
- Prettier is not part of `npm run lint`, and many existing files are not Prettier-formatted. Formatting is not enforced.

## Defects for others
- None found.
