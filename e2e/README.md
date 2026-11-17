# NEXORA end-to-end tests (Playwright + axe-core)

QA/Security-owned. Runs against the full stack behind Caddy (`http://localhost:8080`).

```bash
cd e2e
npm install                       # installs only inside e2e/
npx playwright install chromium   # one-time browser download
docker compose up -d              # from the repo root (+ `npm run dev` in frontend/)
npm test                          # all suites
npm run test:smoke
npm run test:a11y
E2E_BASE_URL=http://localhost:3000 npm test   # another target
E2E_SKIP_IF_DOWN=1 npm test                   # skip (instead of fail) when the stack is not running
npm run typecheck
```

| Suite | What it covers |
|---|---|
| `tests/smoke.spec.ts` | Sign up through the UI → org dashboard (`/o/<slug>`) shows the `synthetic_only` policy → Datasets → upload `testdata/generated/sales.csv` → parse preview → load (classified synthetic) → dataset detail reaches a ready state with 5,020 (or 5,000 de-duplicated) rows. Also: an unknown org slug renders a not-found page and its API returns 401/403/404. |
| `tests/accessibility/a11y.spec.ts` | axe-core WCAG 2.1 A/AA on `/login`, `/signup`, overview, datasets and settings, plus after a theme toggle; zero `serious`/`critical` violations. Runs on desktop and at 375 px (`mobile-375` project). |

Selectors use accessible roles and labels (UI_UX_SPECIFICATION §3–5), never CSS classes, so they double as an
accessibility contract for the frontend. Test users are synthetic (`*@example.test`).
