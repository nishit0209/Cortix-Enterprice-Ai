You own `backend/apps/{ai,nl2sql,documents,ml}/**`.

A. **Vector fallback** (CONTRACT "Local runtime"). The user's local PostgreSQL 17 on Windows has NO pgvector, and everything must still work.
   - Add `apps.ai.vectors.EmbeddingField(dim)`: a custom Field whose `db_type` is `vector(dim)` when the effective backend is pgvector, otherwise `double precision[]`.
     - The effective backend follows `settings.VECTOR_BACKEND`: `"pgvector"`, `"array"`, or `"auto"` (detect whether the `vector` extension is installed or available, and cache the answer per connection).
     - Values convert from/to lists or numpy arrays on both backends.
   - Add `nearest(queryset, field, query_vec, k, *, max_scan=20000)`.
     - pgvector: SQL cosine distance.
     - array: load at most `max_scan` candidate vectors (the caller has already filtered by org/dataset) and rank them with numpy. Return the objects with a `distance` attribute.
   - REWRITE the nl2sql and documents migrations in place: no environment has applied them yet.
     - Use `EmbeddingField`.
     - Create the `vector` extension and the HNSW indexes ONLY when the backend is pgvector. Use RunPython that checks `pg_available_extensions` and `VECTOR_BACKEND`, and make it reversible.
     - The `documents` tsvector/GIN full-text index stays: it is native PostgreSQL.
   - Switch the nl2sql semantic cache and documents hybrid search to `nearest()`. Fusion with full-text search must work on both backends.
   - Unit-test both backends: the field's `db_type`, conversions, and numpy ranking. Write DB tests too.

B. **Local embedding model availability.** `intfloat/multilingual-e5-base` downloads from Hugging Face on first use, about 1.1 GB.
   - Add a management command `download_models` that prefetches it and reports where it is cached.
   - When the model is unavailable (offline, or not downloaded yet):
     - documents still process, extract and chunk, with chunks marked `embedding_pending`;
     - search falls back to full-text only, and the response carries a clear `search_mode`;
     - the nl2sql semantic cache is skipped; the exact cache and the builder still work.
   - Add a `reembed_pending` task, registered with `apps.core.scheduler.register_periodic("ai.reembed_pending", 600, fn)`. Import the scheduler lazily: the Backend agent builds it in parallel, so skip on ImportError.
   - Never crash a request because the model is missing.

C. **Quota UX** (CONTRACT "W3 additions").
   - `GET /ai/status` adds `{quota: {state: ok|low|exhausted, resets_at, used_today, limit_today}, configured, tier}`.
   - nl2sql blocked proposals for quota carry `resets_at`, so bi can emit it.
   - On first exhaustion each day, call `apps.core.notifications.notify` for org admins. Import it lazily; skip if missing.

D. **NL→SQL quality.**
   - Improve the deterministic builder using QA's golden set (`backend/tests/ai_eval/golden_sales.yaml`; read only). Handle g39 and common phrasings: top N, by month/quarter/year, growth vs previous period, share of total, average per X, filters on dimension values with fuzzy matching, "this year / last year".
   - Report builder-only accuracy before and after. Never special-case question strings: use general rules.
   - Add a management command `record_cassettes --out <dir>`. It runs ONLY when `GEMINI_API_KEY` is set and records Gemini responses for the golden questions the builder declines, into the cassette format QA's runner reads (check `backend/testing` / `tests/ai_eval`). Never run it in tests.

E. **Insights.**
   - Make the local insights engine (`compute_insights` / `narrate_template`) produce useful, correct text for the common chart shapes: trend with % change, top contributors, anomalies (z-score) and share.
   - Add unit tests with exact expected outputs.

F. Keep the documents API shapes from the W2 handoff stable (the frontend relies on them). Run all your tests and report REAL counts.
