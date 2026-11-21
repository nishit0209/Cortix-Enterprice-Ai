# W5 handoff: frontend

## Done
- **Customer 360** (`/o/[org]/customers`, `/customers/[id]`), using the routes in `apps/customers/views.py`:
  - Tabs: customers (search and segment filter, cursor paging), review queue, segments, sources, runs & scoring, matching settings.
  - Review queue: the two records side by side with differing values highlighted, per-feature scores, and confirm or reject with a note.
  - Segments: a rule builder (all/any conditions; the field and operator lists mirror `matching.py`), RFM generation, refresh, delete, and the member list.
  - Sources: a mapping dialog that picks columns from the dataset. A `column_not_allowed` error lists the masked or denied columns.
  - Scoring: churn/CLV with model selection. A 409 `ml_model_required` shows a "promote a model" notice. Features can be published.
  - Settings: thresholds, weights and survivorship.
  - Profile page: golden record with survivorship rules, linked records, timeline (event-type filter, paging), and score cards with explanations (contributions/reasons/text) plus history.
- **Product 360** (`/products`, `/products/[id]`):
  - Masters list with search and brand/category filters.
  - Semantic search with a `search_mode` banner. It explains `model_unavailable` and `not_indexed`.
  - Match review with raw and normalized values side by side.
  - Corrections list with approve/reject.
  - Sources mapping, normalize/enrich runs with live job progress, and run history.
  - Detail page: quality ring with completeness/validity/consistency and issue labels, attributes with a "corrected" marker, source records, corrections, and "Propose a correction".
- **Automation** (`/automation` tabs workflows/runs/approvals/agents, `/automation/workflows/[id]`, `/automation/runs/[id]`), aligned with `apps/automation` as built in the backend worktree:
  - Builder on @xyflow/react. The palette comes from `GET automation/steps`, with the CONTRACT list as fallback. Forms are generated from each step's JSON schema, with resource pickers for `*_id` fields.
  - Graph saved in the server dialect: `{steps:[{key,kind,name,config,depends_on,when,retries,retry_backoff_s,timeout_s}], layout}`. A condition's yes/no handles map to `when`.
  - Validation runs on the client and the server. Server errors (`graph.<step>.<field>`) are shown on the step they belong to.
  - Save version, publish (the live version comes from `current_version`), activate/pause, delete, and run with JSON input.
  - Triggers:
    - Schedule: cron presets and a description.
    - Webhook: the secret is shown once, with the URL and the signature header/format. Secrets can be rotated.
    - Event: the 4 CONTRACT events.
    - Every trigger can be paused and resumed.
  - Run page: step timeline polled while active, outputs, `output_redacted` notice, cancel, and a link to approvals when the run is `waiting_approval`.
  - Approvals inbox: status filter, link to the subject, and approve/reject with a comment. High impact requires the password, and a `reauth_failed` error is shown on the password field. Decide buttons only appear for holders of `approver_permission`.
  - Agents: tool allowlist, per-tool `tool_scopes` (`key = v1, v2`), per-tool approval (high impact is always required), and running an agent with a goal.
- **ML promotion**: a production promotion that returns 202 `approval_id` now shows an "Approval requested" banner linking to the inbox.
- **Documents**:
  - Collections: create, filter the library, ask within a collection, and assign one on the detail page (`PATCH documents/{id} {collection_id}`).
  - Extract tab: invoice/receipt/resume, with values, confidence and page citations. It reads `GET documents/{id}/extractions` when that endpoint exists.
  - Compare tab: section diff with similarity and summary.
  - "Save as dataset" on detected tables (`POST document-tables/{id}/promote`).
- **Performance**: `components/lazy-charts.tsx` loads recharts and @xyflow/react with `next/dynamic` and shows sized skeletons.
  - Covers dashboard home charts, dataset profile histogram, the dataset lineage tab, ML charts, Ask/dashboard result charts and the workflow canvas.
  - Light helpers were moved out of the chart modules (`ml/format.ts`, `ask/chart-meta.tsx`).
- Nav: C360, P360 and Automation are now `prototype`. Nothing is "Planned" any more, and the no-mock test now allows no Planned modules.

## Tests (real)
- vitest: **32 files, 288 tests passed, 0 failed** (final run).
  - New tests: MDM helpers, workflow graph/catalog/mappers, document-extras mappers, the approvals inbox (re-auth and wrong password), and the C360 review queue (decision and cursor paging).
  - One earlier full run under heavy machine load had 1 timing failure; it did not reproduce in 2 reruns.
- eslint: pass. tsc --noEmit: pass. next build: pass (all routes built).
- check_conventions OK; check_ownership OK (frontend); manage.py check: no issues; makemigrations --check: no changes. No owned Python, so no ruff or pytest runs.

## Requests
- ai_ml: confirm these document routes, or tell me the real paths:
  - `GET documents/{id}/extractions`;
  - `PATCH documents/{id}` accepting `collection_id`;
  - `document_count` on collections.
  The UI tolerates their absence. Response aliases handled: `extraction_result` wrapper; compare `sections|diff` with `change|status|op`.
- backend: optional `last_run` on workflow list items would fill the "Last run" column. It shows "—" without it.

## Open issues
- The extraction review step (approve/correct an extraction) has no UI yet; its route isn't defined in API_SPECIFICATION.

## Defects for others
- None found.
