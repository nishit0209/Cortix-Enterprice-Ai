# Handoff — frontend — wave 3

Branch: `agent/frontend/w3`   Base: `main` (all wave-1 and wave-2 work merged)
Contract: `contracts/api/CONTRACT.md` ("W2 as-built additions", "W3 additions", "Local runtime"). The client was also checked
against the backend code on `main` and the in-progress W3 branches (read-only): `agent/backend/w3` (`apps/bi` dashboards,
`apps/core` notifications and files) and `agent/ai_ml/w3` (`/ai/status` quota).

Owned paths touched: `frontend/**` and this handoff only. `check_ownership.py --agent frontend --base main` → OK.

## Done

Commits: `6a9a41e`, `43f03c2`, `c682bf8`, `f6753f3`, `3846acf`, `6a92f31`, `24a07cf` (+ this handoff).

### A. No-mock audit (`3846acf`, `24a07cf`, guard in `6a92f31`)

I searched `frontend/src` for hardcoded arrays, sample or demo data, placeholder numbers, fake KPIs, lorem ipsum, `Math.random` and
`setTimeout` fakes. No screen renders invented data: every KPI, list and chart reads an API query (`/data-stats`, `/datasets`,
`/ai/status`, `/ai/usage`, `/activity`, `/insights`, `/bi/query-history`, `/dashboards`, ...), each with loading, error,
unavailable and empty states.

Changes:

| What | Before | Now |
|---|---|---|
| Members role filter (`components/settings/members-view.tsx`) | A hardcoded list of the 7 system roles | The org's roles from `GET /roles`, including custom roles. The system catalog is shown only while the request loads. |
| Idempotency key (`lib/queries/pipelines.ts`) | `Math.random` fallback | `crypto.getRandomValues`. Application code no longer has any random-number source. |
| Home "Recent questions" (`app/o/[org]/dashboard/page.tsx`) | Every successful question was flagged with a badge. The UI expected the verdict `allowed`, but the backend stores `ok`. | Shows the real verdicts (see E). |
| Empty states without a next action | Quality "No profiled datasets yet"; Lineage "Nothing to trace yet"; pipeline runs "No runs yet"; home "No pipeline runs this week"; home "No activity yet" | Each has a button: "Upload a dataset", "Open builder", "Open pipelines" or "Upload data", shown only when the user has the permission. |
| Planned modules (`lib/nav.ts`) | ML, C360, P360 and Automation showed the phase "Later" | Phase taken from `docs/FEATURE_STATUS.md`: ML workspace and Access policies Wave 4, Customer 360 and Product 360 Wave 5, Automation Wave 6. All remain badged "Planned". |
| Dashboards nav entry (`43f03c2`) | Planned | Prototype, backed by the real `/dashboards` API. |

These items were audited and left unchanged, because they are not invented data:
- The `setTimeout` calls are UI timers: copy feedback, filter debounce, SSE watchdog, retry backoff and object-URL revoke.
- "Acme Analytics" appears only as an input placeholder hint.
- The Ask AI example questions are derived from the selected dataset's profiled columns.
- The onboarding next steps, auth value props, chart palettes and option lists (timezones, SSL modes, step catalog) are static UI copy or configuration.

**Guard:** `src/test/no-mock.test.ts` fails the build when any of the following appears:
- Application code under `src/` contains one of these markers:
  - lorem ipsum;
  - a `mock|fake|dummy|sample|demo|seed` + `Data|Rows|Users|Kpis|Stats|…` identifier;
  - `Math.random`;
  - a fake person;
  - a `setTimeout` that resolves with literal data;
  - a hardcoded money figure or `±N%` delta;
  - a fake-data library;
  - a TODO stub.
- A nav entry has the status "mock".
- "Planned" is used outside {ml, customers, products, automation, policies}, or a Planned module has no "Wave N" phase.
- A route renders `PlannedModule` for a module that is not planned.
- `dependencies` contains a mock or fake-data package.

I checked the guard by planting `export const sampleRows = [[1, 2]]` in `src/lib`: the test failed and named the file and line. The probe file was then removed.

### B. Dashboards UI (`43f03c2`, `f6753f3`)

- **`/dashboards` list.**
  - Create, rename, share or unshare, "Show on home" (`is_default`) and delete. Only the owner sees these actions; teammates' shared dashboards are badged "Shared with you".
  - The list request is `?limit=200` with the cursor envelope.
- **`/dashboards/{id}` board.**
  - Layout: a 12-column CSS grid with no new dependency. Widgets are dragged by the title and resized from the corner. With the keyboard, the arrow keys move a widget and Shift+arrow resizes it.
  - Collisions push other widgets down, then the grid compacts.
  - Positions are persisted with `PATCH /dashboards/{id} {layout:[{id,x,y,w,h}]}`. The update is optimistic and rolls back on error, and nothing is sent when no position changed.
  - Bounds mirror `apps.bi.dashboards.clean_layout`: integers, `x + w <= 12`, `1 <= h <= 24`, `y <= 1000`.
  - On small screens the widgets stack in reading order.
- **Widgets** (chart, table, KPI, text).
  - Each data widget fetches `GET /dashboards/{id}/widgets/{wid}/data` with a refresh button, a loading state and the "refreshed" time.
  - Error states: permission (403/404), deleted query (409 `widget_query_missing`), guard rejection under the viewer's policy (422 with the guard copy), service unavailable, and a generic error.
  - The truncation note and the "no rows" message come from the response. Text widgets make no request.
  - Add a widget from a saved query (`GET /saved-queries`) or as a text note; edit or remove a widget (`PATCH`/`DELETE /widgets/{wid}`).
- **Pin to dashboard** on every Ask AI answer that has a result: `POST /dashboards/{id}/widgets {title, kind, message_id, chart, layout}`.
  - The user can also create a new dashboard from the same dialog.
  - Only the caller's own dashboards are offered, because a teammate's shared dashboard answers 403.
  - Gated on `dashboards.manage`, the codename every dashboard and widget route checks. `dashboards.create` only covers saved queries.
- **Org home.** The org home renders the home dashboard read-only, picked by `lib/dashboards/home.ts`:
  - `is_default` is per owner, so the caller's own default comes first;
  - otherwise the most recently updated shared default;
  - otherwise nothing is shown.

### C. Notifications bell (`c682bf8`, `f6753f3`)

- `GET /notifications?limit=30`. The badge count is the envelope's `unread_count` (shown as "99+" above 99), and the bell shows a list with links.
- Mark one read with `POST /notifications/{id}/read`, and all read with `POST /notifications/read-all`.
- Both actions are optimistic through a pure reducer and reconcile with the `unread_count` the server returns.
- The list polls every 30 s (only while the tab is visible) and refetches when the window regains focus.
- Producer links are resolved under the org.
- Resource paths are mapped to the pages that show them:
  - `/settings/ai` → `/settings/ai-policy`;
  - `/pipelines/...` → `/data/pipelines/...`;
  - `/datasets/...` → `/data/datasets/...`;
  - `/data-sources/...` and `/pipeline-runs/...` → their list pages.
- `/o/<org>/` links keep their org. Off-site, protocol-relative and `javascript:` links are refused.

### D. Ask AI (`c682bf8`, `f6753f3`)

- The composer is a `<textarea aria-label="Ask a question">`. A test drives it by that accessible name.
- **Quota banner** (`components/ask/quota-banner.tsx`):
  - Shown above the composer when `/ai/status` `quota.state` is `low` or `exhausted`, with a live countdown to `quota.resets_at`.
  - When the countdown reaches zero, the status is refetched once and the banner clears.
- **AI status pill.** It reads the W3 fields:
  - `quota` (state, used/limit, `rate_limited`, the per-user share) and `configured`, `tier` and `models`;
  - the policy downgrade reason and the limiter fallback note;
  - the reset countdown when the quota is low or exhausted.
- The BI `error` event `ai_quota_exhausted` uses its own `resets_at`.
- SSE handling is unchanged and follows the contract: one POST, `:` keep-alives ignored, and recovery through `GET /messages`.

### E. Match the real backend (`6a9a41e`, `f6753f3`, `24a07cf`)

The client was checked against the views, serializers and URLs in `backend/apps/*` on `main`:

| Area | What was matched |
|---|---|
| Data sources | Import body `{table, schema, …}`; test result mapped to checks; stored catalog from `GET /introspect`; `/imports` |
| Pipelines | Builder ↔ backend DAG adapter (`source.dataset` / `destination.dataset`, config names, derive expression tree); `POST /pipelines/validate` per-node errors; `POST /preview` with `node_id` + `limit` (the DAG is cut at the node); runs, steps, logs, retry; schedule `enabled` |
| Lineage | `?node_type&node_id&depth` → `{nodes:[{id,type,label}], edges:[{from,to,label}]}` |
| Quality | `/datasets/{id}/quality`, `/quality-rules` |
| Semantic | `GET`/`PATCH /datasets/{id}/semantic-model` (minimal diff); dataset metrics CRUD |
| Documents | Page, chunk, entity, table and search shapes; `ask` `mode` with `reason`; citations with page; allowed upload types; original download through the signed URL |
| BI | Feedback `{value: up\|down}`; query history; exports; saved queries; insights envelope |

Fixed in this run:
- Query-audit verdicts are `ok | rejected | error | blocked` (`apps/bi/models.py:21`). They have labels and tones, and the history filter uses the same labels.
- The W3 `dashboard` SQL source has a label.
- `StatusBadge` covers the remaining backend states: `paused`, `archived`, `scanning`, `loading`, `retired`, `rejected` and `disabled`.

The W3 shapes were verified against the backend W3 code:
- Dashboard summary and payload: `is_shared`, `owner_id`, `widget_count`, `created_at`.
- Widget payload: `dataset_id`, `layout`, `text`.
- Widget data: `columns`, `rows`, `row_count`, `truncated`, `chart`, `refreshed_at`, `query_audit_id`, `dataset_id`.
- Notification fields, plus the `unread_count` returned by the list envelope, `/read` and `/read-all` (`{updated, unread_count}`).
- The `/ai/status` `quota` block.

### F. Downloads (`6a9a41e`)

- Exports use `POST /exports`. The response is either the file stream or JSON with `{download_url|url}`.
- Document originals use the returned URL.
- Same-origin signed `/api/v1/files/<token>` URLs are fetched, so an expired link can be explained. Cross-origin presigned URLs are opened by the browser.
- Only https or same-origin URLs are followed.
- An expired link (403, or the 410 `download_expired` the local files view actually returns) raises `download_expired`. Exports then ask for a fresh link once.

### G. Tests

New test files:
- `lib/dashboards/dashboards.test.ts` (14 tests):
  - layout serialization (PATCH body exactly `[{id,x,y,w,h}]`, JSON round-trip);
  - stored layouts clamped to the backend bounds, and 200 seeded move/resize gestures that never overlap or leave the grid;
  - push-down, compaction, placement, pixel→cell and keyboard mapping;
  - home-dashboard choice, KPI extraction, chart-config precedence and `/data` error states.
- `components/dashboards/widget-card.test.tsx` (8 tests):
  - KPI, table, chart and empty results rendered from a stubbed `/widgets/{id}/data`;
  - refresh re-runs the request; the 404, 409 and 422 states render;
  - a text widget makes no request.
- `lib/notifications.test.ts` (11 tests): the reducer, the badge label and link resolution.
- `components/ask/quota-banner.test.tsx` (7 tests):
  - the live countdown to `resets_at` with fake timers, and a single refetch at zero;
  - the low, ok and unconfigured states;
  - quota derivation and the status-pill health.
- `test/no-mock.test.ts` (6 tests): the no-mock guard (see A).
- `lib/chat/error-copy.test.ts` (3 tests): verdict and source mapping.

## Tests (real results, `D:\nexora-worktrees\frontend\frontend`, 2026-11-20)

| Check | Result |
|---|---|
| `npm run lint` | clean (0 problems) |
| `npm run typecheck` | clean |
| `npm test -- --run` | **15 files, 171 passed**, 0 failed, 0 skipped. Before this wave's resumed work: 9 files, 122 tests. The 6 new files hold 49 tests (14 + 8 + 11 + 7 + 6 + 3). |
| `npm run build` | OK: 35 app routes plus middleware |
| `scripts/check_conventions.py` | OK |
| `scripts/check_ownership.py --agent frontend --base main` | OK |
| `ruff check` | All checks passed. Frontend owns no Python. |
| `manage.py check` (test settings) | No issues |
| `makemigrations --check --dry-run` | No changes detected. The migration-history consistency check warned, because no `nexora_migrator` role exists yet on the local PostgreSQL. |
| Backend pytest | Not executed: frontend owns no Python tests. |

Nothing has run against a live backend, because no database exists yet. The UI flows are verified by unit and component tests and by the build.

## Requests

1. **Integrator.**
   - CONTRACT "W3 additions / Dashboards" documents `GET /dashboards` as a bare list. `apps/bi/views.py` (`DashboardListView.get`) returns the cursor envelope `{items, next_cursor, previous_cursor}`; the client accepts both. Please update the contract wording.
   - Notifications: please document the producer link convention, an app-relative UI route such as `/data/pipelines/<id>/runs/<run_id>`. The client maps the resource-style paths listed in C.
2. **Integrator.** Mission A allows "Planned" for C360, P360, ML, Automation and the admin console. "Access policies" (the governance UI) is also still Planned, because `docs/FEATURE_STATUS.md` schedules "governance UI" for W4; the backend governance API exists. The guard allow-list includes `policies`. Please confirm W4, or schedule it.

## Open issues

- **Live run pending.** The real end-to-end check against `runserver` + PostgreSQL waits for the database.
- **No grid library.** Dashboard drag and resize is a dependency-free CSS-grid implementation (no grid library in `package.json`), so no dependency request was needed.
- **Default dashboard in the pin dialog.** The dialog preselects the caller's own default dashboard. Teammates' shared dashboards are view-only.

## Defects for other agents

1. **data_pipelines.**
   - `backend/apps/pipelines/serializers.py:31-33`: `PreviewSerializer` has no `node_id`. CONTRACT "W2 as-built" says preview takes an optional `node_id` and `limit`; DRF silently drops the field, so a step preview runs the whole DAG unless the client cuts it. The frontend cuts the DAG at the node and also sends `node_id`.
   - Not fixed on `agent/data_pipelines/w3` as of this handoff.
2. **data_pipelines.**
   - `backend/apps/pipelines/signals.py:5,23-30`: a run failure only sends an email. CONTRACT "W3 additions / Notifications" lists "pipeline run failed/succeeded (owner)" as an in-app producer through `apps.core.notifications.notify`. Suggested link: `/data/pipelines/<pipeline_id>/runs/<run_id>`.
   - Not wired on `main` or `agent/data_pipelines/w3` as of this handoff.
3. **backend.**
   - `backend/apps/core/notifications.py:6-7`: the docstring example link `/pipelines/<id>/runs/<run_id>` is not a UI route (`/data/pipelines/...` is). It is mapped client-side; please steer producers to UI routes.
   - `backend/apps/bi/views.py:270-271`: saved-query writes gate on `dashboards.create`, while pinning (`POST /dashboards/{id}/widgets {message_id}`, `dashboards.manage`) also creates a SavedQuery. A role holding only `dashboards.manage` can therefore create saved queries through a pin but not directly. Please confirm this is intended.
