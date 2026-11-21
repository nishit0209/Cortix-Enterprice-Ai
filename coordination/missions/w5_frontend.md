Owned: frontend/**. No mock data; every screen reads the API.
A. Customer 360 screens: sources mapping, resolution runs, review queue (confirm/reject with side-by-side records), customers list + profile
   (timeline, scores with explanation, segments), segments builder, scoring runs.
B. Product 360 screens: sources, normalize runs, masters list/detail (attributes, quality score), match review, corrections approve/reject,
   semantic search (search_mode banner). Routes: see coordination/handoffs/w4/data_engineering.md.
C. Automation: a workflow builder on @xyflow/react using the step catalog (GET the registered step kinds if exposed; otherwise the CONTRACT list),
   triggers (schedule / webhook with secret shown once / event), runs + step timeline, an approvals inbox with decision and re-auth for high impact,
   and agents config. ML promotion to production shows "approval requested".
D. Documents extras: collections, extract, compare, table promote.
E. Polish and performance: route-level code splitting / dynamic imports for heavy charts and canvases, so pages load fast in production.
F. Tests (vitest), then lint, typecheck, vitest and build with real results.
