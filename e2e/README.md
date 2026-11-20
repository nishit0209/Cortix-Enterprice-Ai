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
| `tests/smoke.spec.ts` | Sign up through the UI (`/signup`, "Create workspace") → `/o/<slug>/dashboard` shows the "Synthetic only" policy → sidebar "Datasets" (`/o/<slug>/data/datasets`) → "Upload dataset" dialog: `testdata/generated/sales.csv` via the Dropzone, name, classification "Synthetic" + attestation → "Upload and process" → live job stream completes → "Open dataset" → detail shows Ready, 5,020 (or 5,000 de-duplicated) rows, preview rows and schema → sidebar "Ask AI" asks "total revenue by region" and expects SQL + the 5 regions. While `/ask` is still the Planned placeholder on main, the question step is recorded as a `not-exercised` annotation (never faked). Also: a second user sees "Organization not found" for another org, the API returns 404 to that non-member and 401 anonymously, and an unknown dataset id shows "Dataset not found". |
| `tests/accessibility/a11y.spec.ts` | axe-core WCAG 2.1 A/AA on `/login`, `/signup`, overview, datasets, Ask AI, AI policy and settings, plus dark and light themes via the account menu; zero `serious`/`critical` violations. Runs on desktop and at 375 px (`mobile-375` project). |

Selectors use accessible roles and labels (UI_UX_SPECIFICATION §3–5), never CSS classes (the only test id is the
Dropzone's hidden `dropzone-input`), so they double as an accessibility contract for the frontend. They follow the
frontend as merged on main (`frontend/src/components/**`, `frontend/src/lib/nav.ts`). Test users are synthetic (`*@example.test`).
