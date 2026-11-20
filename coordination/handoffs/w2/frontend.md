# Handoff — frontend — wave 2

Branch: `agent/frontend/w2`   Base: `main` (all wave-1 work merged)   Contract: `contracts/api/CONTRACT.md` (W2 + W3 + "W1 as-built additions"), `backend/nexora/contracts.py`

Owned paths touched: `frontend/**` only (plus this handoff). `check_ownership.py --agent frontend --base main` → OK.

## Done
Commits: `3c17a6b`, `b6b0655`, `79a52eb`, `02c2a6e` (+ this handoff).

- **A. Ask AI workspace** (`/o/[org]/ask`, `src/components/ask/*`, `src/lib/chat/*`, `src/hooks/use-chat.ts`, `src/lib/post-stream.ts`)
  - Conversation rail: new / rename / delete, grouped by date (Today / Yesterday / Previous 7 days / Previous 30 days / Older); dataset picker limited to `ready` datasets.
  - Live stream: one-shot fetch POST to `/bi/conversations/{id}/messages` with `Accept: text/event-stream` (never re-POSTs on drop;
    reconnect recovers by refetching the persisted message). Pure reducer for `status/sql/result/insight/token/done/error` with
    ordering rules (late/duplicate events ignored, terminal states sticky).
  - Answer cards: animated stage stepper (authorizing → resolving → generating → validating → executing → explaining), SQL transparency
    panel (highlighted SQL, source badge cache/builder/llm/manual, copy, "Open in SQL editor"), result DataTable, auto chart
    (bar/line/area/pie/kpi from the chart hint with validation + fallbacks, type switcher, themed palette), insight bullets,
    assumptions, follow-up chips, thumbs up/down feedback, CSV/XLSX export via `/exports`.
  - Error cards per code: `ai_policy_blocked` (explains policy, links to settings/ai-policy), `ai_quota_exhausted` (live `resets_at`
    countdown), `ai_not_configured`, every `GUARD_REJECT_CODES` entry with friendly copy + the rejected SQL.
  - Composer: Enter send, Shift+Enter newline, `/` focuses the composer, ↑ (empty input) recalls last question, Esc stops a running stream; example questions derived from
    dataset columns. AI status pill/budget reused from the shell.
  - `/o/[org]/ask/sql`: guarded manual SQL editor (gated on `bi.sql`) via `POST /bi/queries`. `/o/[org]/ask/history`: query history
    table (verdict, source, duration, rows, filters).
- **B. Data Engineering**
  - `/data/sources`: list, "Add PostgreSQL source" dialog with test-connection flow, introspection browser (schemas → tables → columns),
    import-table dialog with job progress (`src/components/jobs/job-progress.tsx`).
  - `/data/pipelines` list/new; `/data/pipelines/[id]` builder on `@xyflow/react`: palette (sources, all 13 transform types, destination),
    typed per-node inspector forms, static schema inference, inline validation errors on nodes, save version, dry-run preview,
    run now with job progress, schedule editor (cron presets + custom). `/data/pipelines/[id]/runs` + run detail (step timeline,
    rows in/out, logs, retry).
  - `/data/lineage` graph (layered auto layout, colored node types, restricted nodes), `/data/quality` overview (scores per dataset,
    failing rules). Dataset detail: new **Semantic** tab (descriptions, synonyms, roles, AI exposure, metrics; publish) and **Lineage** tab.
- **C. Documents** (`/o/[org]/documents`, `/documents/[id]`): drag-drop upload with byte + job progress, list (status/classification/pages),
  detail with page viewer, chunks, entities, tables, hybrid search with safe highlighting, Ask-your-documents panel (citation chips →
  page, "extractive (policy)" badge).
- **D. Dashboard**: insights panel on `GET /insights` (with "Ask about it" links), recent questions on `GET /bi/query-history`, KPI deltas
  computed from real data only (hidden when there is no previous period), AI usage sparkline.
- All endpoints not yet on main render a graceful "not available yet" state on 404/501 instead of failing.
- API types written by hand from the contract in `src/lib/types.ts` (no generated client yet).

## Tests (real results, run in `frontend/`)
- `npm run lint` → 0 errors, 0 warnings
- `npm run typecheck` → clean
- `npm test -- --run` → **9 files, 120 tests passed, 0 failed** (chat reducer ordering/error/reconnect, chart-hint mapping, DAG
  parse/serialize/validate/infer/layout + cron, AnswerCard error cards, stepper, composer shortcuts, thread merge, document answer
  reducer, highlighting, dashboard delta helpers, plus wave-1 suites)
- `npm run build` → success (Next.js 15.5.29, all app routes compiled/generated)
- Backend: no backend code changed. `manage.py check` → no issues; `makemigrations --check` → no changes. Infra (Postgres :5433)
  unreachable → no DB tests were needed or executed.
- `scripts/check_conventions.py` → OK; `scripts/check_ownership.py --agent frontend --base main` → OK.

## Requests
- Backend (W3 Conversational BI): please keep the SSE event names/payloads exactly as in the contract (`status` with `stage`,
  `sql` with `{sql, source}`, `result` with `{columns, rows, row_count, truncated, chart_hint}`, `insight`, `token`, `done` with
  `message_id`, `error` with problem `code`), and the persisted assistant message must carry the same fields so a dropped stream can be
  recovered with `GET /bi/conversations/{id}/messages`.
- Data Engineering: `POST /pipelines/{id}/validate` should return per-node errors that identify the node id (the builder pins them on
  nodes); `POST /pipelines/{id}/preview` should accept the optional `node_id` + `limit` the builder sends; `GET /lineage` must mark
  nodes the caller cannot see as restricted (as in the contract) so the graph can render them as locked.
- AI/ML (documents): Q&A answers should include `mode: "extractive"` when the policy forbids the LLM, and citations with `page` numbers.

## Open issues
- Everything was built against the contract; endpoints that land this wave on other branches have not been exercised end-to-end
  against a live backend (infra down). Run an integration smoke test after merge.
- No generated OpenAPI client yet; `src/lib/types.ts` must be kept in sync by hand until one exists.

## Defects for other agents
- None found from the frontend side in this wave.
