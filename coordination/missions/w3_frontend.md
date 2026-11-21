You own `frontend/**` only.

**THE RULE:** when the user creates the DB and logs in, EVERY number, list and chart must come from the API, which reads the database.

A. **No-mock audit.**
   - Search `frontend/src` for hardcoded arrays, sample or demo data, placeholder numbers, fake KPIs, lorem ipsum and setTimeout fakes.
   - Replace each with real API data and polished empty states. A good empty state explains the next action, for example "Upload a dataset", with a button.
   - "Planned" placeholders may stay ONLY for modules not built yet (Customer 360, Product 360, ML workspace, Automation, admin console), and must be clearly badged "Planned".
   - List everything you changed in the handoff.

B. **Dashboards UI** (CONTRACT "W3 additions" Dashboards).
   - List / create / rename / delete dashboards.
   - Dashboard grid with drag and resize, persisted through PATCH `layout`. Use only dependencies already in `package.json`; if you truly need a grid library, request it and implement a solid CSS-grid alternative now.
   - Widget renderers for chart / table / kpi / text, each fetching `/widgets/{id}/data`, with refresh, loading, error and permission states.
   - A "Pin to dashboard" action on every Ask AI answer, through the `POST /widgets` `message_id` flow.
   - The default dashboard shows on the org home dashboard.

C. **Notifications bell** wired to the API: unread count, a list, mark read and mark all read. Poll every 30 s, plus refetch on window focus.

D. **Ask AI.**
   - Give the question composer an accessible label: a `textarea` with `aria-label="Ask a question"`, so QA's e2e can drive it.
   - Show a quota banner with a live countdown to `resets_at` when the state is low or exhausted.
   - The AI status pill uses the new `/ai/status` fields.
   - Keep the SSE handling exactly per the contract.

E. **Match the real backend.**
   - Check every API call against the merged backend code on main (serializers, views, urls in `backend/apps/*`), not only the contract. Cover data sources, imports, pipelines (validate / preview with `node_id` / runs / retry / schedule), lineage, quality, semantic, documents (`ask` `mode` `extractive|generative` with `reason`; citations with page), BI, insights, query history, exports and saved queries.
   - Fix the frontend side. List backend mismatches as defects with file:line.

F. **Downloads.** Exports and document files use the returned URL, which is a signed `/api/v1/files/...` URL on the local runtime. Handle the 403 an expired link returns.

G. **Tests.**
   - vitest for dashboards layout serialization, widget data rendering, notifications reducer, quota countdown, and a no-mock guard test (fail if `src` contains known demo-data markers you define).
   - Run `npm run lint`, `npm run typecheck`, `npm test -- --run` and `npm run build`, and report the real results.
