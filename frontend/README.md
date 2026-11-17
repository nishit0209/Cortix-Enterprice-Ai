# NEXORA web app

Next.js 15 (App Router) · React 19 · TypeScript · Tailwind v4 · TanStack Query/Table · Radix · Framer Motion · Recharts.

## Run

```bash
npm install
API_INTERNAL_URL=http://localhost:8000 npm run dev   # /api/* and /healthz are proxied to Django
```

| Script | Purpose |
|---|---|
| `npm run dev` | Dev server on :3000 |
| `npm run build` / `npm start` | Production build / serve |
| `npm run lint` | ESLint (next/core-web-vitals + typescript) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test -- --run` | Vitest (jsdom + Testing Library) |

Environment: `API_INTERNAL_URL` (default `http://localhost:8000`), `SESSION_COOKIE_NAME` (default `sessionid`),
`NEXT_PUBLIC_MAX_UPLOAD_MB` (default 200, mirror of the backend `MAX_UPLOAD_BYTES`).

## Layout

```
src/app/                 routes: /login /signup /onboarding /invitations/accept /o/[org]/...
src/components/ui/       design system (tokens in app/globals.css)
src/components/shell/    app shell: sidebar, top bar, org switcher, command menu, notifications, AI pill
src/components/<module>/ feature components (datasets, settings, dashboard, auth)
src/lib/api.ts           fetch wrapper: credentials, CSRF, problem+json -> ApiError
src/lib/sse.ts           resumable SSE client (Last-Event-ID, heartbeat watchdog)
src/lib/queries/         TanStack Query hooks (keys always include the org slug)
src/lib/types.ts         types mirroring contracts/api/CONTRACT.md
```

## Conventions

- Colors only through tokens (`bg-surface`, `text-fg-muted`, `border-border`, `bg-accent`, `--chart-1..8`); both themes are
  defined in `globals.css`.
- Every data view has loading (skeleton), empty, error (problem `code` + `request_id`) states. Never show sample numbers:
  modules that aren't built use `PlannedModule`.
- Permissions: use `useOrgContext().can("codename")` for UI gating; the server is authoritative.
