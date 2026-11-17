# Handoff — ai_ml — wave 1

Branch: `agent/ai_ml/w1`   Base: `fd484d9`   Contract version: CONTRACT.md as of wave 0

## Done
- **A. AI models** → `backend/apps/ai/models.py`, `migrations/0001_initial.py`
  - `AIRequestLog`: TenantModel, RLS and `MakeAppendOnly`. It records feature, model, prompt_key/version, payload classes and assets, effective ai_data_policy, gemini_tier, priority, input/output/cached tokens, latency_ms, attempts, status (ok | rate_limited | quota_exhausted | blocked_by_policy | invalid_output | error | not_configured), error_code, prompt_sha256 and request_id. It also has `prompt_text`, which holds the redacted text and is filled only when `log_prompt_text` is on.
  - `AIUsageDaily`: org + Pacific day + model, with F() increments. RLS.
  - `AIOrgConfig`: defaults are 50 requests/day, 250k tokens/day, 20 requests per user, and log_prompt_text=False. RLS. Every UniqueConstraint includes `org`.
  - `AIModelQuota`: GLOBAL table with model, rpm, tpm, rpd and verified_at.
- **B. GeminiService** → `apps/ai/engine.py` (`GeminiEngine`). `apps/ai/services.gemini()` is the singleton that implements `nexora.contracts.GeminiService`.
  - Methods: `generate_structured`, `generate_text`, `stream`. Policy, config, breaker and quota checks run eagerly for streams too.
  - `apps/ai/gemini.py` is the **only** module that imports `google.genai`. It contains the SDK adapter:
    - JSON mode with `response_mime_type=application/json` + `response_schema`
    - system instruction, temperature, max tokens and a per-request timeout
    - `usage_metadata` accounting (thoughts tokens count as output)
    - error classification: 429 per-minute vs per-day (QuotaFailure quotaId), `RetryInfo.retryDelay`, 5xx and timeouts
  - Structured output is checked with a JSON parse plus a local schema validator (`apps/ai/schema.py`). There is **one** repair call (only if budget remains), then `AIInvalidOutput` (`ai_invalid_output`, 502). Safety blocks map to `ai_invalid_output` with `reason=safety`.
  - Model tiers come from `GEMINI_MODEL_FAST` / `GEMINI_MODEL_REASONING` (and `GEMINI_MODEL_VISION` if set).
  - An empty `GEMINI_API_KEY` raises `AIUnavailable(code=ai_not_configured)` and logs `not_configured`. There is no fallback.
  - Structured logging uses logger `nexora.ai`, one JSON line per request. It never contains the API key or the prompt.
  - The prompt registry is in `apps/ai/prompts/` (`nl2sql.generate`, `nl2sql.repair`, `bi.narrate`), with versions and declared payload classes. A payload class the prompt does not declare is refused.
  - `request_trace()` collects AIRequestLog ids, used for `SqlProposal.ai_request_ids`.
- **C. Quota & resilience** → `apps/ai/quota.py`
  - The Redis limiter is atomic: WATCH, then MULTI/EXEC, retried on WatchError.
  - It enforces project RPM/TPM/RPD per model, read from AIModelQuota with a 10% margin. Defaults when no row exists: flash-lite 15/250k/500, other models 5/250k/20.
  - It also enforces the org daily request and token budget and the per-user cap, with priority shares: interactive 100%, assistive 60%, background 30%.
  - Retries use 1s/2s backoff with +25% jitter, raised to `retryDelay`, max 3 attempts. A wait longer than 20s is never slept; the minute breaker opens instead.
  - Circuit breaker states: `open_minute` and `open_day`. `open_day` lasts until the next midnight America/Los_Angeles (DST-aware) and exposes `resets_at`.
  - If Redis is down, AI fails closed with `ai_limiter_unavailable`.
- **D. AIDataPolicy gate** → `apps/ai/policy.py`
  - `check_policy(payload)` uses `ai_policy_state()`, `classify_asset()` and `GEMINI_TIER`, exactly as in §5.1/§5.3.
  - Garbage policy values, missing consent, and aggregates_only/full on the free tier all collapse to `synthetic_only`.
  - Unknown classifications are treated as confidential, and unknown payload classes are refused.
  - A block raises `AIPolicyBlocked` before any network call, and the attempt is logged as `blocked_by_policy`.
  - The redactor (`apps/ai/redaction.py`) runs on every prompt. It masks emails, Luhn-valid card numbers, Aadhaar-like numbers, PAN-like IDs and phone numbers.
- **E. Local embeddings** → `apps/ai/embeddings.py`
  - `LocalEmbedder` loads the sentence-transformers model lazily, with the cache dir from `SENTENCE_TRANSFORMERS_HOME`/`HF_HOME`. It applies the e5 `query: `/`passage: ` prefixes, returns normalized float32 vectors, encodes in batches and checks the dimension.
  - `FakeEmbedder` is deterministic, for tests.
  - Helpers: `get_embedder()` and `override_embedder()`.
- **F. Endpoints**
  - `GET /orgs/{org}/ai/status` (`org.view`) returns the CONTRACT W3 shape plus the extra fields listed under Open issues.
  - `GET /orgs/{org}/ai/usage?days=1..90` needs `org.settings.edit` or `audit.view`. Its shape is listed under Requests.
- **G. nl2sql**
  - `services.propose(question, ctx)` resolves in this order: exact cache → semantic cache (pgvector cosine ≥ 0.93, same slot shape) → deterministic builder → Gemini (`nl2sql.generate`) → at most one repair.
  - Other services: `services.repair(...)` (allowlist of repairable codes; security codes are never retried), `services.store_verified(...)` and `services.remember(...)`.
  - `BiQueryCache` is a TenantModel with RLS, a `VectorField(768)`, an HNSW cosine index and a unique constraint on (org, scope_id = dataset_version_id or dataset_id, question_hash).
  - The cache stores pre-policy SQL **templates**. Number and date slots are re-bound with sqlglot, with equality constraints for contiguous periods.
  - Every cache hit is re-checked against the **current user's** allowed columns.
  - Builder questions: top/bottom N, measure by dimension, totals and counts (including distinct counts), time series by grain, and this-vs-last week/month/quarter/year. Matching uses rapidfuzz (OSA typo distance) and dateparser for time phrases. It declines instead of guessing when it meets unrecognized words.
  - Gemini prompt contents:
    - the schema card of allowed columns only, with PII columns aliased to `pii_col_N` and mapped back locally
    - metric definitions
    - a value-free history (questions, SQL shapes with literals replaced by `?`, and column lists)
    - the rules and today's date
    - the payload declaration `SCHEMA` on `dataset:<id>`
  - When the AI path is blocked or out of quota, `propose` returns `SqlProposal(source="none", blocked_reason=...)` after the cache and builder have been tried.
- **H. Local insights** → `apps/nl2sql/insights.py`
  - `compute_insights(columns, rows)` returns top_n, share, period_change, anomaly (robust z-score) and trend (linear fit + R²), using pandas/numpy. Masked columns are ignored.
  - `narrate_template(...)` writes the narrative with real numbers.
  - `narrate_with_gemini(...)` streams, and is gated on SCHEMA + VALUES for the asset.
  - `narrate(...)` falls back to the template.
- **Test doubles for other agents**: `apps/ai/testing.py` provides `FakeTransport` (a network spy), `FakeRedis`, `MemoryRecorder` and `build_engine()`. Use `apps.ai.services.override_gemini(engine)` to swap the engine.

## Tests (real counts)
Command, run from `backend/`:

```
$env:PYTEST_DB_SUFFIX="ai_ml"; python -m pytest apps/ai/tests apps/nl2sql/tests --create-db -p no:cacheprovider
```

- **Unit tests (no DB): 557 passed, 0 failed.**
  - Policy matrix: 288 cases (4 policies × 4 classifications × 3 tiers × consent × 3 payload classes) plus 18 edge cases.
  - Engine: 33 (repair, retries, breaker, quota, not-configured, redaction, logging, streaming).
  - Transport adapter: 24. Quota/breaker: 16. Redaction: 16. Schema/timeutil/registry/import-hygiene: 16. Embeddings: 6.
  - Builder: 51 (34 question variants, all parsed with sqlglot). Slots/SQL utils: 46. propose/repair: 21. Cache logic: 7. Insights: 15.
- **DB tests: 17 written, NOT EXECUTED (infra unavailable).** Docker, Postgres :5433 and Redis :6380 were unreachable, so all of these errored on DB connect.
  - `apps/ai/tests/test_db.py` (10): DbRecorder, append-only, RLS, global quota, audited config update, end-to-end gate with a durable log row, status/usage endpoints and permissions.
  - `apps/nl2sql/tests/test_cache_db.py` (7): exact/semantic pgvector hits with re-binding, verified precedence, policy re-check, RLS.
- No test calls Gemini live, and no test downloads an embedding model.
- `python scripts/check_conventions.py` → `conventions OK`. `ruff check apps/ai apps/nl2sql` → all checks passed.
- The migrations were generated with `makemigrations` and then given `EnableTenantRLS` (ai_airequestlog, ai_aiusagedaily, ai_aiorgconfig, nl2sql_biquerycache) and `MakeAppendOnly` (ai_airequestlog). **They have not been applied to a real DB yet.**

## Requests (Integrator)
- [setting] Add `GEMINI_MODEL_VISION`, `GEMINI_MAX_OUTPUT_TOKENS` (2048), `GEMINI_VERTEX_PROJECT` and `GEMINI_VERTEX_LOCATION` to `settings/base.py`. They are listed in AI_ARCHITECTURE §3.1 and read with `getattr` defaults today. Also document `HF_HOME` for local runs.
- [contract] Add `GET /orgs/{org}/ai/usage` to CONTRACT.md with this shape:

  ```
  { range: {from, to, days, timezone}, totals: {requests, input_tokens, output_tokens, cached_tokens, blocked, errors}, daily: [{day, model, requests, input_tokens, output_tokens, cached_tokens, blocked, errors}], by_feature: [{feature, requests, input_tokens, output_tokens}], by_status: {status: n} }
  ```

  Query parameter `?days=1..90`. Permission: `org.settings.edit` or `audit.view`.
- [contract] `/ai/status` returns additive fields: `stored_policy`, `consent_valid`, `policy_downgrade_reason`, `token_budget`, `user_budget`, `budget_resets_at`, `models: {fast|reasoning: {model, breaker, resets_at}}`, `limiter_available`.
  - `policy` is the **effective** policy.
  - `resets_at` is the breaker reset time when the breaker is open, otherwise the next Pacific-midnight budget reset.
- [infra] Durable logging of blocked attempts. `AIRequestLog` rows are written in the caller's transaction. If a view lets `AIPolicyBlocked` or `AIQuotaExhausted` propagate, `TenantScopedMixin` rolls back on 4xx and the row is lost; the structured `nexora.ai` log line survives. Two possible fixes:
  - a dedicated DB alias for autonomous log writes (the same nexora_app role, a separate connection);
  - a core helper for "commit despite rollback".

  nl2sql already catches these errors and returns `source="none"`, so its rows persist.
- [backend] `apps.orgs.services.ai_policy_state()` is still the wave-0 stub. The gate relies on `consent_valid`, which must become false when `AI_CONSENT_TEXT_VERSION` changes or consent is revoked. Please keep the return shape.
- [data_engineering] Register asset classifiers: `register_asset_classifier("dataset", ...)` in `apps/datasets/apps.py`, and `"document"` later. Until then every asset classifies as confidential, which fails closed.
- [data_engineering] Align the sqlguard reject codes with `apps/nl2sql/sqlutil.py`:
  - `REPAIRABLE_CODES`: parse_error, not_select, column_not_allowed, table_not_allowed, unknown_column, unknown_table, ambiguous_column, type_error, invalid_sql, execution_error, cost_exceeded.
  - `SECURITY_CODES`: never repaired.
  - `repair()` uses an allowlist, so any new code is refused until it is added.
- [backend/bi] Integration notes for `apps/bi`:
  - After `sqlguard` rejects LLM SQL with a repairable code, call `nl2sql.services.repair(question, raw_sql, reject_code, ctx, reject_message=...)` **once**.
  - After a successful execution, call `nl2sql.services.remember(...)`; on a thumbs-up, call `store_verified(...)`.
  - Use `compute_insights` / `narrate_template` for the `insight` and `explain` SSE events. For streaming tokens, use `insights.narrate(..., prefer_gemini=True)`.
- [ops] Seed `ai_aimodelquota` rows from AI Studio before production. The defaults are conservative.

## Open issues / known limitations
- None of the DB-dependent code has been run against Postgres yet: migrations, pgvector queries, RLS on the new tables, and the endpoints. It is covered only by the 17 unexecuted tests listed above.
- The cache scope uses `dataset_id`, because `QuestionContext` does not carry `dataset_version_id`. `propose()`, `remember()` and `store_verified()` accept an optional `dataset_version_id=` so the bi agent can pass it.
- Retries are in-process (bounded: at most 2 sleeps, each ≤ 20s, otherwise breaker plus error). Celery `countdown` re-queueing on the `llm` queue (§3.2) is left to callers of background work.
- Not done in this wave:
  - the prompt registry is code-only; the `ai_prompt_template` DB seeding and admin activation of versions (§3.3) were not started;
  - the `scripts/verify_gemini.py` live check (Integrator-owned path);
  - `apps/documents` and `apps/ml` (not in this wave's scope).
- `AIModelQuota` has no admin endpoint yet (platform_admin, wave 2+).
- The builder handles only single-table questions. Filters on values ("in the West") and joins go to Gemini by design, so for real datasets under `synthetic_only` they return `ai_policy_blocked`.
