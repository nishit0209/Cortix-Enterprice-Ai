# Handoff — frontend — wave 1

Branch: `agent/frontend/w1`   Base: `fd484d9` (Wave 0)   Contract version: `contracts/api/CONTRACT.md` as of Wave 0

## Done
- **Tooling** → `frontend/package.json`, `package-lock.json`, `vitest.config.mts`, `next.config.ts`
  - Deps: framer-motion, recharts, @tanstack/react-query, @tanstack/react-table (v9 `useTable` + features API), @xyflow/react,
    lucide-react, next-themes, cmdk, sonner, clsx, tailwind-merge, cva, @radix-ui/* (dialog, dropdown-menu, popover, tooltip,
    tabs, select, avatar, switch, checkbox, scroll-area, separator, label, slot), zod, react-hook-form, @hookform/resolvers.
    Dev: vitest, @vitejs/plugin-react, jsdom, @testing-library/{react,dom,jest-dom,user-event}; `@types/node` bumped to ^24
    (vitest 5 peer requirement).
  - Scripts: `typecheck` (`tsc --noEmit`), `test` (`vitest`).
  - `next.config.ts`: rewrites `/api/:path*` and `/healthz` to `API_INTERNAL_URL || http://localhost:8000`, security headers.
- **Design system** → `frontend/src/app/globals.css` (tokens, light/dark, chart palette, type scale, motion), `src/components/ui/*`
  - Button, Input/Textarea, Field/Label, Select, Checkbox/Switch, Badge, Card, Tabs (animated underline), Dialog,
    ConfirmDialog (optional typed confirmation), DropdownMenu, Popover, Tooltip, Skeleton, EmptyState, ErrorState
    (problem `code` + copyable `request_id`), Progress, Kbd, Separator, DataTable (sorting, column filters, global search,
    pagination, column visibility, mobile card list, loading/empty/error/no-match states), StatCard/KpiCard + Sparkline,
    ChartCard (Recharts theme + accessible table toggle), ScoreRing, StatusBadge, FeatureStatusBadge
    (Working/Prototype/Mock/Planned), ClassificationBadge, Toaster (sonner), PageHeader, Breadcrumbs, Avatar, Dropzone.
  - Original NEXORA mark (`src/components/brand/logo.tsx`, `src/app/icon.svg`).
- **API layer** → `src/lib/api.ts`, `src/lib/errors.ts`, `src/lib/types.ts`, `src/lib/sse.ts`, `src/lib/queries/*`, `src/hooks/*`
  - Fetch wrapper: `credentials: "include"`, one-time `GET /auth/csrf` (deduplicated), `X-CSRFToken` on unsafe methods,
    single retry with a refreshed token on Django CSRF failures, problem+json → `ApiError {status, code, detail, errors,
    requestId}`, network failures → `status 0 / network_error`, global 401 listener → `/login?next=…`, XHR uploads with
    byte progress, cursor helpers.
  - Error catalog mirroring API_SPECIFICATION §3 codes.
  - SSE: spec-compliant incremental parser + resumable client on `fetch` streams (sends `Last-Event-ID`, persists it in
    sessionStorage to survive reloads, honours `retry:`, 45 s heartbeat watchdog, bounded reconnects, no retry on 4xx).
    `useEventStream<EventMap>` (typed events) and `useJobStream` (REST snapshot + stream reducer).
    EventSource was not used because it cannot send `Last-Event-ID` after a reload nor stream a POST (needed for W3 chat).
  - TanStack Query hooks for me/auth, orgs, members, invitations, roles, AI policy/consent, ai/status, activity, audit
    (infinite cursor), data-stats, insights, datasets (+columns/preview/quality/classify/delete), jobs. Keys include the org slug.
- **App shell & auth** → `src/components/shell/*`, `src/components/auth/*`, `src/middleware.ts`, `src/app/(auth)/*`, `src/app/onboarding`
  - Middleware: no `sessionid` cookie → `/login?next=` for `/o/*` and `/onboarding`. `OrgGate` validates via `GET /me`, checks
    membership, loads `GET /orgs/{org}` permissions (falls back to a client mirror of SYSTEM_ROLES only for UI gating).
  - Collapsible sidebar (icon rail, persisted, `[` toggles), mobile drawer, permission-filtered sections Overview / Data /
    Intelligence / Customer 360 / Product 360 / Automation / Governance / Admin; org switcher (+ Create organization);
    breadcrumbs; cmdk command menu (Ctrl/Cmd+K: navigation, quick actions, org switch, theme, sign out); notification center
    (GET activity, unread tracking); AI status pill (GET ai/status: policy, budget, breaker, resets_at); user menu.
  - `/login`, `/signup` (creates the org), `/onboarding` (create org when the user has none), `/invitations/accept`,
    root `/` redirect to the last-used org.
- **Screens** (all with skeleton, empty and error states; endpoints that 404 because a wave isn't merged render "not available")
  1. Dashboard: KPIs (datasets, active pipelines, avg quality, AI usage vs budget), data-processed bar chart (rows per dataset),
     7-day pipeline run donut, activity feed, AI insights (policy-aware empty state), quick actions.
  2. Datasets list + upload dialog (drag-drop, POST uploads → POST datasets, live SSE job progress with stage bar and logs,
     classification select + attestation checkbox for synthetic/public). `?upload=1` deep link.
  3. Dataset detail: header (classification chip, version, tags, rows/cols, quality ScoreRing, actions: reclassify with
     attestation, copy IDs, delete with typed confirmation), tabs Preview (masked cells as •••), Schema & profile (per-column
     cards: type, semantic type, PII badge, null %, distinct, min/max/mean/median, histogram or top values), Quality
     (score, four dimensions, rules DataTable, duplicates).
  4. Settings: AI policy (current policy, consent status, four values explained, tier locks, consent dialog with consent
     text + acknowledgement + password re-auth, revoke with confirm), Members (role change inline, invite dialog, remove with
     ConfirmDialog, pending invitations + revoke, `?invite=1`), General (rename org), Audit log (DataTable, server filters
     `action`/`target_type`, cursor "load older", JSON detail dialog).
  5. Every other module route (`data/sources|pipelines|quality|lineage`, `ask`, `dashboards`, `documents`, `ml`, `customers`,
     `products`, `automation`, `governance`) renders a Planned state with the module's scope. No fake data anywhere.
- **Tests** (`frontend/src/**/*.test.ts(x)`):
  - `npm test -- --run` → **5 files, 48 tests, 48 passed** (api client CSRF/problem+json/401/network/list helpers; SSE parser,
    stream reconnect with Last-Event-ID, 4xx stop, retry budget, job reducer; `useEventStream` hook; DataTable pagination,
    sorting, search, empty/loading/error, row click; ScoreRing, badges, StatCard/Sparkline, ConfirmDialog, Dropzone,
    helpers).
  - `npm run typecheck` → pass (0 errors). `npm run lint` → pass (0 errors, 0 warnings). `npm run build` → pass (27 routes).
  - `python scripts/check_conventions.py` → `conventions OK`.
  - Manual visual QA in the browser pane (1440 px and 375 px, no horizontal scroll) against a throwaway synthetic mock API
    in my scratchpad (not committed). **Not run against the real Django backend:** the W1 auth/org endpoints are being built on
    another branch.

## Requests (Integrator)
- [contract] `GET /orgs/{org}/activity`: please pin the shape. The UI accepts a bare array or the `{items}` envelope and reads
  `actor_email` (optionally `actor_name`) on each audit event.
- [contract] `POST /orgs/{org}/datasets`: the UI also sends `attest: true` when `classification` is `synthetic`/`public` (the
  attestation checkbox). Please accept and audit it, or tell me to drop it.
- [contract] `POST /ai-policy/consents` with a wrong password: please return `errors: [{field: "password", …}]` or
  `code: "invalid_password"` so the dialog can mark the field (both are handled).
- [contract] Pin numeric scales: `quality_score`, `avg_quality`, quality `dimensions` and `stats.null_pct` (the UI accepts
  0–1 or 0–100) and one `stats.histogram` encoding (the UI accepts `[{start,end,count}]`, `[{bin|label,count}]` or
  `{bins,counts}`).
- [contract] `POST /invitations/accept`: the UI treats 404/410 as "invitation expired or revoked".
- [infra] Frontend container env: `API_INTERNAL_URL=http://<backend-service>:8000`. `next/font/google` fetches Inter and
  JetBrains Mono at **build time** (network needed during `next build`). Optional `SESSION_COOKIE_NAME` (default `sessionid`)
  and `NEXT_PUBLIC_MAX_UPLOAD_MB` (default 200 = `MAX_UPLOAD_BYTES`).
- [infra] In production, route `/api/*` straight to Django at the ingress (large uploads, SSE without buffering); the Next
  rewrite is meant for dev and simple deployments.
- [ci] Add `npm ci && npm run lint && npm run typecheck && npm test -- --run && npm run build` in `frontend/` to CI.

## Open issues / known limitations
- No Playwright e2e here (`e2e/**` belongs to QA). Integration against the real backend still needs to happen after merge.
- Types are hand-written from CONTRACT.md. The spec's generated `openapi-typescript` client isn't set up because there's no
  `contracts/openapi.yaml` yet.
- Not yet implemented from the UI spec: density modes, next-intl message catalogs, storybook/ladle stories, virtualized tables,
  Lineage tab and dataset Versions/Access tabs (their APIs land in W2+).
- The UI kit lives in `src/components/ui` as the task asked (the spec mentions `src/ui`).
- `npm audit` reports 2 transitive vulnerabilities (1 moderate, 1 high) from the scaffold's dependency tree. I didn't run
  `npm audit fix --force` because it makes breaking upgrades.
- eslint 9.39 prints a deprecation notice during install (scaffold version, unchanged).
