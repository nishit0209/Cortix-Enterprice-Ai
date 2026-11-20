# Handoff — ai_ml — wave 3

Branch: `agent/ai_ml/w3`   Base: `ce5969a` (main with all wave-1/2 work)   Contract: CONTRACT.md "Local runtime", "W2 as-built additions", "W3 additions"

## Done

### A. Vector fallback (PostgreSQL without pgvector)
- `apps/ai/vectors.py`
  - `EmbeddingField(dim)`: the column is `vector(dim)` when the effective backend is pgvector, otherwise `double precision[]`.
  - Values convert from/to lists, tuples, numpy arrays and pgvector/array text. Dimension and finiteness are checked; reads return float32 numpy arrays.
  - `effective_backend()` follows `VECTOR_BACKEND` = `pgvector | array | auto`. `auto` means "is the `vector` extension installed?", cached per physical connection.
  - At runtime `field_backend()` uses the column type that actually exists, so a database migrated without pgvector keeps working.
  - `nearest(queryset, field, query_vec, k, *, max_scan=20000)`:
    - pgvector: SQL `<=>` cosine distance (HNSW).
    - array: loads at most `max_scan` candidates of the caller-filtered queryset (newest first) and ranks them with numpy.
    - Results carry `.distance`.
  - Migration helpers `ensure_vector_extension()` / `hnsw_index()`: reversible RunPython steps that check `pg_available_extensions` and `VECTOR_BACKEND`.
    - The extension and HNSW indexes are created ONLY on pgvector.
    - In `auto` mode a failed `CREATE EXTENSION` (no superuser) falls back to array. A forced `pgvector` fails with a clear message.
- Migrations rewritten in place (never applied): `nl2sql/0001_initial`, `documents/0001_initial`. There is exactly one 0001 per app. `makemigrations --check` is clean.
  - Both use `EmbeddingField` with the extension/HNSW RunPython steps.
  - The documents tsvector + GIN full-text index is unchanged.
  - New: `DocumentChunk.embedding_pending` (partial index) and `Document.embedding_status` (`none|complete|pending`).
- The nl2sql semantic cache and documents hybrid search use `nearest()`. RRF fusion with full-text works on both backends.

### B. Local embedding model availability
- `LocalEmbedder` loads from the local cache only (`EMBEDDING_CACHE_DIR` / `SENTENCE_TRANSFORMERS_HOME` / `HF_HOME` / HF default).
  - Request paths never download. They raise `EmbeddingUnavailable` (reason `model_not_downloaded|load_failed|library_missing|misconfigured`), with a 5-minute cooldown after a failed load.
- `python manage.py download_models [--check] [--reembed] [--json] [--model]`: prefetches `intfloat/multilingual-e5-base` (~1.1 GB), warms it up and prints the cache directory and model path.
  - Exit codes: 0 when ready, 1 when unavailable.
  - Checked live here: `--check` reports `missing` (the model is not cached on this machine).
- When the model is unavailable:
  - documents still process, extract and chunk; chunks are stored `embedding_pending`, the document is `embedding_status: "pending"` with a warning;
  - search is full-text only, and the response says so;
  - the nl2sql semantic cache is skipped, while the exact cache and the builder still work;
  - nothing crashes.
- `apps/ai/reembed.py`: a registry of `Reembedder(scan_sql, run)`. Two are registered: `documents.chunks` and `nl2sql.cache`.
  - `run_reembed_pending` scans orgs on the `ops` alias (BYPASSRLS, ids only) and embeds each org in `tenant_context`.
  - It downloads in the background only when `EMBEDDING_AUTO_DOWNLOAD` (default true).
  - Registered as periodic `ai.reembed_pending` (600 s) through a lazy `apps.core.scheduler.register_periodic` import. It is skipped on ImportError and is compatible with Backend W3's scheduler (`fn()` called without arguments).

### C. Quota UX
- No Redis: the limiter runs on `ai.AIQuotaCounter` (migration `ai/0002_quota_counter`, a global table with no tenant data).
  - `apps/ai/quota_store.DbRedis` implements the redis-py subset that `QuotaLimiter` uses.
  - A reservation is one transaction on the durable alias. It inserts placeholder rows, locks them `SELECT … FOR UPDATE` and re-inserts any placeholder that was purged in between. Reporting reads take no locks.
  - The hourly purge `ai.purge_quota_counters` is registered only without `REDIS_URL`.
  - `_default_limiter()` uses Redis when `REDIS_URL` is set, else this store.
- `GET /ai/status` adds `quota: {state ok|low|exhausted, resets_at, used_today, limit_today, remaining_today, scope, rate_limited, user}`, plus `limiter_backend` and `embeddings`. `configured` and `tier` were already present.
  - `scope` is one of `platform | org_requests | org_tokens | user | null`.
  - "low" means at most 20% of a budget is left.
  - `resets_at` is the breaker's time when the day breaker is open, otherwise the next Pacific midnight.
- nl2sql quota/rate-limit blocks return `BlockedProposal` (a `SqlProposal` subclass) with `resets_at`, `retry_after_s` and `quota_scope`.
  - `nl2sql.services.blocked_details(proposal)` returns the extra error-event fields.
  - Backend W3 bi already reads `getattr(proposal, "resets_at")`, so they are compatible.
- On the first org-level exhaustion of each Pacific day (`org_day`, `org_tokens`, `project_day`), the members holding `org.settings.edit` are notified once.
  - It goes through `apps.core.notifications.notify_permission` (lazy import, falling back to `notify` + `orgs.services.members_with_permission`).
  - It runs in a savepoint and is best effort. The link is `/settings/ai-policy`. Personal caps never notify.
- Documents notify the uploader with `document.processed` / `document.failed` (lazy, savepoint).

### D. NL→SQL quality
- The builder uses general rules only:
  - a bare entity noun is counted (g39 "weekly orders");
  - superlatives ("which 3 products sold the most units", "channel with the lowest revenue");
  - share of total ("share of revenue by region", "what percentage of revenue comes from each channel");
  - growth series ("revenue growth by month", "mom revenue", "quarterly revenue growth");
  - growth between periods ("revenue growth this year vs last year" → current, previous, change_pct);
  - average per entity ("average revenue per order", "revenue per customer by region");
  - two grouping dimensions;
  - "this/last year" with every shape.
- Dimension-value filters (`apps/nl2sql/filters.py`):
  - Known values from column metadata: exact match first, then a one-edit fuzzy match for values of 5+ characters. A value that fits several columns is declined unless a column name disambiguates it; OR-filters are declined.
  - Named values: `<in|for|from…> [the] <value> <dimension>`.
  - Never on PII or masked columns. No known values on tables with a row-level policy (the named rule still works there).
- `apps/nl2sql/values.py`: `propose()` feeds the builder the dataset profile's top values (`DatasetColumn.stats.top_values` via `apps.datasets.services`).
  - Only inside the tenant context, in a savepoint, cached per (org, version).
  - They never reach the Gemini schema card (tested).
- Builder-only accuracy on QA's `golden_sales.yaml`:

  | Run | Result |
  |---|---|
  | **Before (main)** | **43/48 = 89.6%** |
  | **After (QA's context, as the runner uses it)** | **45/48 = 93.8%**: g39 and g45 fixed. g46–g48 need dimension values the golden context does not carry. Precision when answered: 100% |
  | **After, with profiled values in the context (as production provides)** | **48/48** (test `test_golden_set_builder_accuracy_with_profiled_values`) |

- `python manage.py record_cassettes --out <dir> [--only g46,g47] [--force] [--layout flat|nl2sql] [--max-wait]` (`apps/nl2sql/management/commands/record_cassettes.py`):
  - It runs only with `GEMINI_API_KEY`; otherwise it exits 2 and calls or writes nothing.
  - For each golden question the builder declines, it runs the real LLM path (`nl2sql.generate`, local validation, at most one repair) with QA's golden context and anchor date.
  - It uses `apps.ai.services.standalone_engine()`: the real policy gate, in-memory pacing at the model's default limits, rate-limit waits, no database.
  - It writes `nexora.cassette.v1` (`format, id, question, prompt_key, model, recorded_at, repaired, response`).
  - A repaired answer is stored as the generate response, so QA's replay yields the executed SQL.
  - Replay: `NEXORA_CASSETTES_DIR=<dir> python -m testing.golden --with-cassettes`.
  - It was NOT run (no key here). Its tests replay the written cassette through QA's `testing.golden.cassette_sql` and get the correct result.

### E. Insights
- `compute_insights` / `narrate_template` cover:
  - a trend with % change and in-progress periods;
  - top contributors with shares;
  - robust z-score anomalies;
  - share of total;
  - both compare shapes (long and wide).
- The old "total total amount" duplication is fixed.
- 23 exact-text tests in `test_insights_w3.py`.

### F. Documents API stability
- The W2 routes and shapes are unchanged; fields are only added:
  - `search` / `ask`: `search_mode` (`hybrid|fulltext|none`), `search_mode_reason` (`embedding_model_unavailable|embeddings_pending|vector_search_failed|null`), `search_mode_message`, and `retrieval.{mode, reason, pending_embeddings}`;
  - document `embedding_status`;
  - chunk `embedding_pending`.

## Tests (REAL results, 2026-11-20)
Command, from `backend/`: `$env:PYTEST_DB_SUFFIX="ai_ml"; $env:DB_CONNECT_TIMEOUT="2"; python -m pytest apps/ai/tests apps/nl2sql/tests apps/documents/tests -p no:cacheprovider`

- **Unit tests (no DB): 844 passed, 0 failed.** That is 161 more than W2's 683.

  | Suite | Passed | Not executed (DB) |
  |---|---|---|
  | `apps/ai/tests` | 505 | 17 |
  | `apps/nl2sql/tests` | 250 | 9 |
  | `apps/documents/tests` | 89 | 23 |

  Every limiter scenario in `test_quota.py` now runs on both FakeRedis and the PostgreSQL store semantics.
- **DB tests: 49 written, NOT EXECUTED (no database).**
  - Reason: a PostgreSQL answers at localhost:5432, but `password authentication failed for user "nexora_migrator"`. The user's database and credentials do not exist yet.
  - 38 are from W2. 11 are new:
    - `apps/ai/tests/test_w3_db.py` (3): the counter table backs the limiter; purge; `/ai/status` quota block.
    - `apps/nl2sql/tests/test_w3_cache_db.py` (2): exact cache without the model, then `reembed_missing`, then a semantic hit; column type and HNSW index match the backend.
    - `apps/documents/tests/test_w3_db.py` (6):
      - upload without the model → pending → full-text search → `reembed_pending` → hybrid search;
      - complete with the model;
      - `nearest` cosine ranking and distances on the live backend, plus the RLS check;
      - bounded array scan;
      - column type and indexes match the backend;
      - the transactional periodic job across two orgs.
- QA suites on this branch:
  - `tests/ai_eval`: 67 passed, 3 skipped (g46–g48, no cassettes), **1 failed: `test_golden_question[g39]` XPASS(strict)**. g39 is fixed but still carries `known_gap` (see Requests).
  - Whole `tests apps` failure set compared with main: identical except that g39 XPASS.
  - The other 10 failures also fail on main: the sqlguard corpus `mask-002..012`, `setop-009`, and the DB-only GUC test.
- Checks:
  - `ruff check apps/ai apps/nl2sql apps/documents apps/ml`: OK.
  - `manage.py check`: no issues.
  - `makemigrations --check`: no changes.
  - `check_conventions`: OK.
  - `check_ownership --agent ai_ml --base main`: OK.
- No test calls Gemini, downloads a model or needs pgvector, Redis or tesseract.

## Requests
- **[qa_security]** Remove `known_gap` from g39 in `backend/tests/ai_eval/golden_sales.yaml`.
  - The builder now answers it correctly, so the strict xfail XPASSes and fails `tests/ai_eval/test_golden_sales.py::test_golden_question[g39]`.
  - g45 can become `builder: true`. g46–g48 are answered by the builder when the context carries profiled values; for the runner's context they still need cassettes (see below).
- **[user / Integrator, needs a Gemini key]** Run `python manage.py record_cassettes --out <dir>` once a key exists, then `NEXORA_CASSETTES_DIR=<dir> python -m testing.golden --with-cassettes`. Free tier is enough: 3 questions, paced.
- **[settings, Integrator]** Add these to `base.py` and `.env.example`. The code reads them with `getattr` defaults.
  - `EMBEDDING_CACHE_DIR` (default empty = HF cache).
  - `EMBEDDING_AUTO_DOWNLOAD` (default `true`; the background re-embed job may download the model).
  - `DOCUMENT_VECTOR_MAX_SCAN` (default 20000).
- **[contract, Integrator]** Record these in CONTRACT.md:
  - the `/ai/status` quota extras (`remaining_today, scope, rate_limited, user`), `limiter_backend` and `embeddings`;
  - documents `search_mode*`, `retrieval.mode/reason/pending_embeddings`, `embedding_status` and `embedding_pending`;
  - `BlockedProposal.resets_at/retry_after_s/quota_scope` and `blocked_details()`.
  - Optionally add `resets_at: str | None = None` to `nexora.contracts.SqlProposal`.
- **[data_engineering, optional]** Put profiled `top_values` as `values` into `DatasetSemantics.columns_meta` for non-PII dimension columns. nl2sql fetches them itself meanwhile (`apps/nl2sql/values.py`).
- **[frontend]** Documents:
  - show `search_mode_message` when `search_mode == "fulltext"`;
  - show a "semantic search pending" badge for `embedding_status: "pending"`;
  - `/ai/status` `quota.scope` can refine the quota banner copy.
- **[ops / final guide]** After creating the DB, run `python manage.py download_models` once (~1.1 GB). Until then search is full-text only and the background job fetches the model when `EMBEDDING_AUTO_DOWNLOAD` is true.

## Open issues / known limitations
- None of the DB-backed code has run against PostgreSQL yet. That covers:
  - the migrations on both vector backends;
  - `nearest`;
  - the counter table under concurrency;
  - documents pending → re-embed;
  - the `/ai/status` quota block via HTTP.
  It is covered only by the 49 unexecuted tests.
- Array backend: ranking considers the newest `max_scan` (20,000) candidates per filtered scope. Corpora larger than that need pgvector (bootstrap installs it when available).
- `CREATE EXTENSION vector` needs a superuser unless pgvector is trusted. `scripts/bootstrap_local_db.py` creates it. In `auto` mode the migrations fall back to `double precision[]`.
- The real e5 model has not been downloaded or loaded on this machine; model loading is tested with stubs. The first semantic request after a server start loads the model, which takes a few seconds on CPU.
- `record_cassettes` was not run (no key).
- The value-filter lookup (`values.known_values`) is unit-tested with stubbed `apps.datasets.services`; no DB test.

## Defects for other agents
- **[qa_security]** `tests/ai_eval/test_golden_sales.py::test_golden_question[g39]` fails as XPASS(strict) because `golden_sales.yaml` g39 still has `known_gap`. The gap is fixed in `apps/nl2sql/builder.py`. Remove the field.
- **[data_engineering / qa_security]** Pre-existing on main, not caused by this branch: `tests/security/sql/test_sql_guard_corpus.py::test_guard_verdict[mask-002, -003, -004, -007, -008, -009, -010, -012, setop-009]` fail.

## Review fixes (independent review of agent/ai_ml/w3)

Fixed (each with regression tests; no DB needed):
- **[high] Background download held `LocalEmbedder._lock`** (`apps/ai/embeddings.py`). `download()` now fetches and loads outside `_lock`, under its own `_download_lock`; only installing the finished model takes `_lock`.
  - While a download runs, `available` / `status()["available"]` are False, and embed calls raise `EmbeddingUnavailable(reason="model_downloading")` at once.
  - Request paths take `_lock` non-blocking. A concurrent load from the cache gives `model_loading` immediately, with no cooldown.
  - A Hugging Face snapshot with no weights file (`*.safetensors` / `*.bin`), i.e. a download still in progress in any process, no longer counts as cached.
  - Tests: `test_request_path_never_waits_for_a_background_download`, `test_concurrent_cache_load_degrades_instead_of_blocking`, `test_partial_snapshot_is_not_cached`.
- **[medium] `ai.reembed_pending` ran inline in the scheduler tick** (`apps/ai/reembed.py`, new `apps/ai/tasks.py`).
  - The registered fn is now `enqueue_reembed_pending()`. It returns at once and hands the run to `apps.ai.tasks.reembed_pending`, an ops task on `NexoraTask`, queue `ml`. While `NexoraTask` is missing (Backend W3 not merged yet), it hands the run to a daemon thread instead.
  - `_run_lock` still prevents overlapping runs.
  - Each org is embedded `COMMIT_EVERY` (64) rows per `tenant_context` transaction, up to `batch`.
  - Tests: `test_periodic_fn_only_enqueues_the_task`, `..._falls_back_to_a_daemon_thread...`, `test_reembed_commits_each_slice_in_its_own_tenant_transaction`, `test_reembed_task_is_an_ops_task_on_the_ml_queue`.
- **[medium] Share insight called the sum of a LIMITed top-N "the total"** (`apps/nl2sql/insights.py`, `sqlutil.py`, `services.py`).
  - `compute_insights(..., complete=None|True|False)`: only `complete=True` says "X% of the total revenue (1,000)" and puts percentages in the top-N listing.
  - Unknown or incomplete results say "X% of the combined revenue of the N regions shown (…)". The data carries `basis: "total"|"shown"`, `total` (None unless complete), `shown_total`, `groups_shown`.
  - A SQL share column (builder `share_pct` = `SUM() OVER ()`, which covers every group) is used when present.
  - `services.compute_insights(columns, rows, *, complete=None, sql=None, truncated=None)` derives completeness through `sqlutil.result_is_complete(sql, row_count, truncated=)`. The result is False when it is truncated, has an OFFSET, has an inner LIMIT, or reaches the outer LIMIT/FETCH. It is True when nothing cut it, and None when this cannot be told.
  - Tests: `test_limited_top_n_is_never_called_the_total`, `test_result_is_complete[*]`, `test_services_compute_insights_derives_completeness_from_the_sql`, plus updated share tests.

Request (backend, owner of `apps/bi`): in `apps/bi/services.py` `_insights()`, call `ports.compute_insights(column_dicts(rs.columns), rows, sql=<the executed SQL>, truncated=rs.truncated)`. Also widen `apps/bi/ports.compute_insights(columns, rows, **kwargs)` to forward them to `apps.nl2sql.services.compute_insights`. Until then BI never claims "the total". It states shares against the rows shown, or uses `share_pct` when the SQL has it.

Tests after the fixes, on `apps/ai`, `apps/nl2sql`, `apps/documents` and `apps/ml`: 912 collected, 863 passed, 0 failed. 49 did not run because there is no database: they error at DB setup with "password authentication failed".

Checks:
- Pass: ruff, `check_conventions`, `check_ownership --agent ai_ml --base main`, `manage.py check`, `makemigrations --check` and lint-imports (2 contracts kept).
- Not run: the frontend checks. The worktree has no `node_modules`, and this branch does not change `frontend/`.
