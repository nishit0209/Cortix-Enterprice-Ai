# UI/UX Specification

> Status: **Design.** Last updated 2026-11-17. Owner agent: Frontend (`frontend/**`).
> **Inspiration** (quality bar, not copying): the calm density of Linear, Vercel's typography and restraint, Stripe's clarity in data tables, and the data-workspace layouts of Databricks, Snowflake and Microsoft Fabric. **No generic admin template.** Nexora has its own design system.

## 1. Design principles
1. **Data first, chrome last.** Neutral surfaces, with color reserved for meaning (status, charts, focus).
2. **Every AI answer is inspectable.** Every AI answer shows its SQL and sources and is honest about policy blocks.
3. **Keyboard-friendly.** `⌘K`/`Ctrl K` opens the command palette, and every primary action has a shortcut.
4. **Progressive disclosure.** Simple defaults, with advanced panels (SQL editor, policy preview, pipeline JSON) one click away.
5. **Honest status.** Prototype and Mock badges are visible on modules that are not yet Working.
6. **Accessible.** WCAG 2.2 AA: contrast ≥ 4.5:1 for text, focus rings everywhere, `prefers-reduced-motion` respected, and charts paired with table views.

## 2. Design tokens (`frontend/src/ui/tokens.css`, CSS variables; Tailwind reads them)

### 2.1 Color (OKLCH-based, light and dark)
| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#FBFBFC` | `#0B0C0F` | App background |
| `--surface` | `#FFFFFF` | `#121419` | Cards, panels |
| `--surface-2` | `#F4F5F7` | `#181B21` | Sidebar, table header |
| `--border` | `#E4E6EB` | `#262A33` | Hairlines |
| `--text` | `#0E1116` | `#E8EAEE` | Primary text |
| `--text-muted` | `#5B6270` | `#9AA1AE` | Secondary text |
| `--accent` | `#4F46E5` (indigo 600) | `#8B87FF` | Primary actions, focus |
| `--accent-soft` | `#EEF0FF` | `#1E1F3A` | Selected rows, AI panel tint |
| `--success` | `#13804B` | `#3DD68C` | Succeeded runs, passed rules |
| `--warning` | `#B25E09` | `#F5A524` | Warnings, quota near limit |
| `--danger` | `#C42B2B` | `#F26D6D` | Failures, blocked |
| `--info` | `#0B6BCB` | `#5AA9FF` | Info, AI-policy notes |
| Chart palette | 8 hues, color-blind-safe (validated with a CVD simulator), same order in both themes | | Series |

### 2.2 Typography
- **Typefaces:** UI is **Inter** (variable, tabular numerals for data); code and SQL are **JetBrains Mono**. Both are self-hosted via `next/font`.
- **Scale:**

| Name | Size / line height | Weight |
|---|---|---|
| `xs` | 12/16 | 400–500 |
| `sm` | 13/20 | 400–500 |
| `base` | 14/22 (dense data UI) | 400–500 |
| `md` | 16/24 | 500–600 |
| `lg` | 20/28 | 500–600 |
| `xl` | 24/32 | 600 |
| `2xl` | 30/38 | 600 |

- Headings use −1% letter spacing.

### 2.3 Space, radius, elevation, motion
- **Spacing:** 4-pt scale (4, 8, 12, 16, 20, 24, 32, 40, 48, 64).
- **Radius:** `6px` controls, `10px` cards, `14px` modals.
- **Elevation:** borders before shadows. One soft shadow token for popovers and modals.
- **Motion (Framer Motion):** 120–180 ms ease-out for UI, 220 ms for panels, layout animations for table/chart switches. All reduced under `prefers-reduced-motion`.
- **Density modes:** comfortable (default) and compact (data-heavy users), stored per user.

## 3. Component library (`frontend/src/ui/`)
Built on Radix primitives (shadcn-style source components, restyled with the tokens above):

| Group | Components |
|---|---|
| Layout | `AppShell`, `Sidebar`, `TopBar`, `PageHeader`, `SplitPane`, `Drawer`, `Tabs`, `EmptyState` |
| Inputs | `Button` (primary, secondary, ghost, danger), `Input`, `Select`, `Combobox`, `MultiSelect`, `DateRangePicker`, `Switch`, `Checkbox`, `CronInput`, `FileDropzone`, `CodeEditor` (CodeMirror 6, SQL + JSON) |
| Data | `DataTable` (TanStack Table: virtualized, column resize, pin, sort, filter, export; masked-cell rendering), `KpiCard`, `StatDelta`, `Sparkline`, `Chart` (Recharts wrappers: Bar, Line, Area, Pie/Donut, Scatter, Combo), `Histogram`, `Heatmap` |
| Feedback | `Toast`, `Banner` (quota, policy), `StatusBadge` (queued, running, succeeded, failed, retrying), `FeatureStatusBadge` (Working, Prototype, Mock), `Skeleton`, `ProgressStream` (SSE) |
| AI | `ChatThread`, `ChatComposer`, `SqlTransparencyPanel`, `InsightCard`, `CitationChip`, `PolicyNotice`, `QuotaMeter` |
| Graphs | `PipelineCanvas`, `LineageGraph`, `WorkflowCanvas` (@xyflow/react with custom nodes) |
| Overlays | `Dialog`, `ConsentDialog` (AI policy), `ReauthDialog`, `CommandPalette` (cmdk), `Popover`, `Tooltip` |

Each component has Storybook-style examples in `frontend/src/ui/__stories__` (Ladle, lightweight) and Vitest + Testing Library tests.

## 4. App shell and navigation

```
┌───────────────┬─────────────────────────────────────────────────────────────┐
│ ◆ Nexora      │ [Org switcher ▾]   ⌘K Search…            AI ● 32/50   ☾  (U) │
│               ├─────────────────────────────────────────────────────────────┤
│ ⌂ Overview    │ PageHeader: title · breadcrumbs · primary action            │
│ ⛁ Data        │                                                             │
│   Sources     │   content                                                   │
│   Datasets    │                                                             │
│   Pipelines   │                                                             │
│   Quality     │                                                             │
│   Lineage     │                                                             │
│ ✦ Ask (BI)    │                                                             │
│ ▦ Dashboards  │                                                             │
│ ⛨ Governance  │                                                             │
│ ── later ──   │                                                             │
│ ☰ Documents   │                                                             │
│ ∿ ML          │                                                             │
│ ◎ Customers   │                                                             │
│ ▣ Products    │                                                             │
│ ⟳ Automation  │                                                             │
│ ⚙ Settings    │                                                             │
└───────────────┴─────────────────────────────────────────────────────────────┘
```

- **Sidebar:** collapsible (icon rail), and items are hidden when the user lacks the permission. Post-MVP modules show a `Planned` badge and stay hidden until built (a feature flag).
- **Top bar:** org switcher, command palette, AI status pill (policy + remaining org budget + breaker state), theme toggle, user menu.
- **Routes** (App Router): `/o/[org]/…`, matching the API org slug, so every URL is shareable and tenant-explicit.
- **Responsive:**
  - ≥ 1280 px: full layout.
  - 768–1279 px: sidebar collapses to a rail.
  - < 768 px: sidebar becomes a drawer, tables switch to card lists, and the pipeline canvas is read-only with a "best on desktop" notice.

## 5. Key screens (MVP)

### 5.1 Auth and onboarding
- Sign-up creates the workspace.
- The onboarding checklist has five steps:
  1. invite a teammate;
  2. upload a dataset (or "load synthetic sample data" for demos);
  3. review quality;
  4. ask your first question;
  5. pin a chart.
- **AI policy step:** explains that the default is `synthetic_only` and shows how to mark datasets as synthetic or public, or (for an owner or admin) how to consent to `schema_only`.

### 5.2 Data sources and upload
- **Sources list:** type icon, status, last test, verified read-only badge.
- **"New source" wizard:** choose type → configure (credential fields are write-only) → test (streamed steps: DNS/IP check, TLS, login, privilege check) → choose tables.
- **Upload:** a dropzone with per-file progress, then parse preview (first 50 rows, detected types editable before load), then confirm.

### 5.3 Dataset explorer
- **Header:** name, classification chip (`synthetic`/`public`/`internal`/`confidential`, with an attestation tooltip), version selector, quality score ring, owner, tags.
- **Tabs:**
  - Preview (policy-aware table; masked cells shown as `•••` with a tooltip);
  - Schema and profile (per-column cards: histogram, nulls, distinct, PII badge);
  - Quality (rules, results, failures sample);
  - Lineage;
  - Access (ACL and policies, for admins);
  - Versions.

### 5.4 Pipeline builder
- **Canvas:** sources on the left, the destination on the right, with a node palette (Clean, Dedup, Reshape, Quality, Join).
- **Node inspector:** a typed form per step, with live output schema and a sample preview (dry run on 1,000 rows).
- **Validation errors** appear inline on nodes.
- **Top bar:** Save draft · Validate · Publish · Run · Schedule (`CronInput` with a human-readable preview, e.g. "Every weekday at 06:00 IST").
- **Runs tab:** a table with status, trigger, attempt, duration, rows in/out/rejected and quality score. The run detail shows a step timeline (Gantt-like), streamed logs (SSE), error panel and retry button.

### 5.5 Ask (Conversational BI)

```
┌ Dataset: Sales (synthetic) ▾ ─────────── Policy: synthetic_only ⓘ ─ AI 32/50 ┐
│ ┌ thread ───────────────────────────────────────┐ ┌ Transparency ───────────┐ │
│ │ You: Top 10 products by revenue this month     │ │ Source: semantic cache  │ │
│ │ Nexora: ▸ status: resolving → executing        │ │ SQL (executed):         │ │
│ │  [Bar chart | Table toggle]                    │ │  SELECT … LIMIT 1001    │ │
│ │  Insight: "Product A leads with ₹12.4L (+18%)" │ │ Tables: orders, products│ │
│ │  Assumptions: "this month = 1–17 Nov 2026 IST" │ │ Policies applied: 2     │ │
│ │  👍 👎  Save · Pin to dashboard · Export       │ │ Rows: 10 · 142 ms       │ │
│ └────────────────────────────────────────────────┘ │ [Open in SQL editor]    │ │
│ [ Ask a follow-up…                          ⏎ ]   └─────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Streaming stages** (`status` events) render as a compact stepper. The chart appears as soon as the `result` event arrives, and narration streams in afterwards (only when policy allows).
- **Policy notices** say plainly what happened. For example: "AI is off for this dataset because it's classified *internal* and your org uses `synthetic_only`. Use the visual query builder, or ask an admin." This appears with a link to the builder.
- **Quota exhausted** shows a banner with the reset time in the user's timezone. Cached and builder answers still work.
- **Query history:** a searchable list with source badges (cache, builder, AI, manual).
- **Visual query builder** (no AI): choose metric(s), dimension(s), filters and time grain → SQL preview → run. It uses the same guard.

### 5.6 Dashboards
- A grid layout (react-grid-layout) of KPI, chart, table and text widgets.
- Widgets come from saved queries or metric definitions, with a global date-range filter and drill-down (click a bar to filter, or open in Ask).
- **Templates:**
  - **BI template** (revenue, sales, customer metrics, operations, cost analysis, KPIs, predictive insights placeholder);
  - **Org Admin template** (§6A).

### 5.7 Governance
- **Policies** per dataset:
  - a column grid with allow, deny or mask per role;
  - a row policy builder using a structured predicate UI with `$user.<attr>` tokens;
  - a **Preview as user** panel that runs a sample query under that user's effective policy.
- **AI data policy page:**
  - current policy, consent history, the four values explained, and which features each enables;
  - the "Grant `schema_only`" button opens a `ReauthDialog` and then the `ConsentDialog`;
  - a revoke button.
- **Audit log** (filters, diff viewer, chain verification badge), **access history**, **retention**, **usage**.

## 6. Dashboards by persona
| Dashboard | Widgets | Phase |
|---|---|---|
| **A. Org Admin** | Org overview (members, datasets, storage); Data sources health; Pipeline health (success rate 7d, failing pipelines, mean duration); Dataset statistics (rows, quality score distribution); AI usage (requests per day vs budget, blocked by policy, cache hit rate); User management shortcuts; Access-control summary; System activity feed (audit) | 2–5 (basic), 11 (full) |
| **B. Business Intelligence** | Revenue, Customer metrics, Sales, Operations, Cost analysis, Business KPIs (from metric definitions), Predictive insights (placeholder until P9, labelled **Planned**) | 4 |
| **C. ML Workspace** | Model performance, training jobs, dataset stats, predictions volume, monitoring (drift) | 9 |
| **D. Platform Super Admin** | All orgs (status, size, activity), org activity, platform health, Gemini project usage vs AI Studio quota + breaker state, infrastructure (CPU, memory, queues), background jobs (failed, retrying), system logs | 11 |

## 7. States, errors and empty states
- **Every data view has four designed states:** loading (skeletons), empty (with an explanation and a primary action), error (with problem `code` and `request_id` copy), and partial (truncated results notice).
- **Error code → message:** a central catalog in `frontend/src/lib/errors.ts` mirrors API error codes, with plain-language text and suggested actions.
- **Long jobs** always show progress from SSE and survive page reloads (resume via `Last-Event-ID`).

## 8. Frontend architecture
- **Rendering:** Next.js App Router. Server components for shells; client components for interactive data views.
- **Data:** TanStack Query for server state, with keys always including the org slug. No global client store beyond UI preferences (Zustand, small).
- **API client:** generated from `contracts/openapi.yaml` (`openapi-typescript`) + a typed fetch wrapper that handles CSRF, problem+json and 401 redirects. **Never hand-edit `src/api/generated/`.**
- **SSE:** an `EventSource` wrapper with `Last-Event-ID` resume, a heartbeat watchdog and typed events validated with zod against `contracts/events/*.schema.json`.
- **Forms:** react-hook-form + zod (schemas derived from OpenAPI where possible).
- **Theming:** next-themes (system, light, dark), with tokens via CSS variables. Charts read the tokens.
- **i18n:** English UI in the MVP, with strings in message catalogs (next-intl) so Hindi and Gujarati UI can be added later.
- **Security:** no secrets in the frontend. Strict CSP compatible (nonce). Markdown rendering sanitized (no raw HTML). Pre-signed downloads.

## 9. UX acceptance criteria (per phase)
1. Lighthouse accessibility ≥ 95 on main pages. axe-core checks in Playwright show 0 serious or critical violations.
2. Light and dark themes pass contrast checks, and charts are distinguishable under a deuteranopia simulation.
3. At 375 px, 768 px and 1440 px there is no horizontal page scroll and primary flows are completable (pipeline editing is desktop-only by design).
4. Every AI answer shows its SQL and source within one click. Every policy block shows the reason and the next step.
5. Every screen built in a phase has Playwright coverage of its primary flow (`TESTING_STRATEGY.md` §6).
