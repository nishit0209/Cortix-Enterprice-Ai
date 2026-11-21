You own `frontend/**`.

**THE RULE (unchanged):** every number, list and chart comes from the API, which reads the database. No mock or demo data.

The routes are those of `docs/API_SPECIFICATION.md` (Phase 9 ML, Phase 5 governance/usage, Phase 11 platform) plus CONTRACT "W4 additions". The backends are being built IN PARALLEL this wave. Code against the contract, and verify shapes against the code on the parallel branches (read-only):
- `D:\nexora-worktrees\ai_ml` (`apps/ml`);
- `D:\nexora-worktrees\backend` (`apps/governance`, `apps/platform_admin`, `apps/orgs` perms).

Keep graceful "not available yet" states for 404/501.

Customer 360 / Product 360 screens come NEXT wave. Keep their nav entries badged "Planned".

## A. ML Workspace (`/o/[org]/ml`) — the flagship of this wave; make it excellent

1. **Projects list.**
2. **New project wizard:** dataset picker (ready datasets) → target + task type (auto-detected, overridable) → features (with `excluded_by_policy` and leakage-dropped columns shown with reasons) → create.
3. **Project page with tabs:**
   - **EDA:** stats table, missingness bars, target distribution, correlation heatmap, top categories, time coverage.
   - **Recommendations:** ranked cards with reasons.
   - **Train:** pick algorithms (presets from the recommendations) and hyperparameters (sane defaults; advanced collapsible), start, then live job progress.
   - **Experiments:** a table with status and metrics; compare 2–4 side by side (metric bars, ROC/PR curves when present, confusion matrix heatmap, residual plot, feature importance, SHAP summary bar chart).
4. **Model registry:** versions, stages, promote (`ml.promote`; confirm dialog).
5. **Prediction playground:** a form generated from the feature schema, single prediction with probability and SHAP top contributions; batch prediction from a dataset → job → link to the output dataset.
6. **API usage panel:** curl example with an API key scope `ml.predict` (never show real secrets).
7. **Monitoring tab:** PSI per feature with thresholds (green < 0.1, amber < 0.25, red), prediction distribution.

Use Recharts and your design system, with light/dark themes and motion. Make it professional.

## B. Governance UI (`/o/[org]/settings/governance` or `/governance`)

- Dataset ACLs.
- Column policies (allow/deny/mask with mask type).
- Row policies: a structured condition builder (`{column, op, value | user_attribute}`), never free SQL.
- User attributes.
- Policy preview: pick user + dataset → shows the allowed/masked columns and the row predicate summary.
- Retention policies with a dry-run result.
- Access history table.

This replaces the "Planned" badge for Access policies.

## C. Admin

- **Org usage** (`/o/[org]/settings/usage`): charts from `GET /usage` and `/ai/usage`, with date-range picker and metrics.
- **Platform console** (`/platform`): only when `me.is_platform_admin` (check `/me`), else 404 page. Orgs table with suspend/reactivate (confirm), health, jobs, AI usage + model quotas form, logs viewer with filters.

## D. Wave-3 leftovers

- Remove the pipeline destination "Append" option unless Data Engineering ships real append this wave; check their branch.
- Send `{schema, table}` separately on import.
- Use the new DataStats/Lineage fields: `sources`, `runs_7d.total`, `datasets_by_status`, `restricted`/`restricted_count`, and `*_prev` deltas when present.
- Documents: show `search_mode_message` when `search_mode` is fulltext; show an "embedding pending" badge.
- Use the original download through `download_url` (AI/ML adds it this wave).
- The signed-link 410 `download_expired` → "link expired, regenerate".
- Keep the e2e accessible names stable, as listed in QA's W3 handoff.

## E. Tests

- vitest for: ML wizard state machine, schema-driven prediction form validation, metrics/curve data mappers, PSI colour thresholds, row-policy condition builder serialization, platform-admin gating.
- Run lint, typecheck, `vitest --run` and build, and report the real results.
