# Handoff — ai_ml — wave 2

Branch: `agent/ai_ml/w2`   Base: `929dcf3` (main with all wave-1 work)   Contracts: `nexora/contracts.py` W2 + CONTRACT.md "W1 as-built additions"

## Done

### A. Wave-1 follow-ups
1. **Durable AI log writes** (`apps/ai/recorder.py`)
   - `DbRecorder` writes `AIRequestLog` and `AIUsageDaily` through the `durable` DB alias. It opens its own `transaction.atomic(using="durable")` and calls `set_db_tenant(org, using="durable")` on that connection, so blocked or failed attempts survive a rolled-back request.
   - `log_alias()` falls back to `default` (a savepoint, the old behaviour) only in two cases:
     - the alias is not configured;
     - `durable` is a test MIRROR of `default` while `default` is held open by a django TestCase transaction. The mirror's separate connection could not see the uncommitted org rows there.
   - Transactional tests use the real separate connection.
2. **Reject codes** (`apps/nl2sql/sqlutil.py`)
   - `REPAIRABLE_CODES` is `nexora.contracts.REPAIRABLE_REJECT_CODES`, and `SECURITY_CODES = GUARD_REJECT_CODES - REPAIRABLE_REJECT_CODES`. The local duplicates are deleted.
   - The local pre-check now emits guard vocabulary only:
     - `forbidden_construct` and `command_not_allowed` became `forbidden_clause`;
     - `system_catalog` and `system_column` became `system_object`.
   - `repair()` refuses every non-repairable code, including unknown ones.
3. **Consent** — the gate was re-verified against the merged `apps.orgs.services.ai_policy_state()`, which still returns `{policy, consent_valid}`. New DB tests use the real ledger:
   - grant `schema_only`, then SCHEMA is allowed and VALUES is still blocked;
   - revoke, then the next request is blocked;
   - `AI_CONSENT_TEXT_VERSION` change, then `consent_valid=False` and the policy is `synthetic_only`;
   - a stored `full` policy on the free tier is refused.
4. **Cache scope via DatasetSemantics** (`apps/nl2sql/services.py`)
   - `propose / remember / store_verified` accept `semantics: DatasetSemantics`, and the scope becomes `semantics.dataset_version_id`.
   - `resolve_version()` rejects semantics or version ids that belong to a different dataset.
   - New helper `context_from_semantics(semantics, policy, user_id=, history=)` builds a `QuestionContext`. It keeps only the user's allowed columns, and drops metrics whose expression mentions a disallowed column.
5. **`python manage.py verify_gemini`** (`apps/ai/management/commands/verify_gemini.py`)
   - Lists models with `GenaiTransport.list_models()`, which is in `apps/ai/gemini.py`. No generation call is made.
   - Checks `GEMINI_MODEL_FAST / _REASONING / _VISION`, including `generateContent` support when it is listed.
   - Options: `--json`, `--require-key`.
   - Exit codes:
     - 0: all models are present, or the run was skipped because no key is set;
     - 1: a model is missing or cannot generate;
     - 2: the listing failed, or `--require-key` was given without a key.
   - Tests inject the lister and never touch the network.

### B. Document Intelligence foundation (`apps/documents`, AI_ARCHITECTURE §7)
- **Models** (all TenantModel + `EnableTenantRLS`; every UniqueConstraint includes `org`):
  - `Document`:
    - basic fields: title, filename, mime, file_kind, size, sha256, storage_key;
    - classification (default `internal`; `synthetic`/`public` need `attest=true`, which records attested_by/at);
    - `doc_type` + scores;
    - status (`processing|ready|failed`);
    - counts and results: page_count, language + script shares, error/error_code, warnings, ocr_pages / ocr_unavailable_pages, chunk_count;
    - extractive summary and last_job_id.
  - `DocumentPage`: text, method (`text|ocr|ocr_unavailable|ocr_failed|empty`), ocr_confidence, language, label (sheet name).
  - `DocumentChunk`: text, page/page_to, section path, token_count, `embedding VectorField(settings.EMBEDDING_DIM)` with an HNSW cosine index, and a stored `search_vector` (`to_tsvector('simple')`, GIN index).
  - `DocumentEntity`: kind, value, normalized, is_pii, confidence, offsets, chunk FK.
  - `DocumentTable`: header, rows (capped), n_rows/n_cols, truncated.
- **Upload safety** (`filetypes.py`):
  - extension allow-list (pdf, docx, xlsx, csv, txt) and magic bytes;
  - size cap: `DOCUMENT_MAX_UPLOAD_BYTES` (default 50 MB), capped by `MAX_UPLOAD_BYTES`;
  - OOXML guard: member count, uncompressed size and ratio (zip bomb), required parts, macro parts rejected, `..` paths, defusedxml parse of `[Content_Types].xml`, and DOCTYPE/ENTITY rejected in every XML part;
  - text files: binary/NUL check.
- **Processing** runs as `TenantTask process_document` on the `ml` queue (via the `apps.documents.*` route), as a long-running task with short `tenant_step`s. Steps:
  1. download from MinIO;
  2. extraction:
     - pdfplumber text, font-size heading detection and ruled tables;
     - pypdf fallback;
     - scanned pages (< 25 text chars) go to Tesseract OCR with whatever of `eng+hin+guj` is installed, after PIL preprocessing;
     - if the tesseract binary is missing, the page is marked `ocr_unavailable` and processing never crashes;
     - python-docx: headings, tables in body order, and page numbers from rendered/explicit page breaks;
     - openpyxl read-only: one page per sheet;
     - CSV with dialect sniffing;
     - TXT with form-feed pages;
  3. language detection by script ranges (`en`/`hi`/`gu`/`mixed`/`und`);
  4. chunking: about 500 tokens with 80 tokens of overlap inside a section. Headings start chunks and build the section path. Tables stay intact, or are split by rows with the header repeated. Page ranges are kept;
  5. local e5 passage embeddings in batches of 32. If the embedder fails, the document stays FTS-only and gets a warning;
  6. regex entities:
     - email, phone (IN/intl/US), dates (normalized ISO, day-first), INR/USD amounts with lakh/crore/million scaling;
     - PAN; GSTIN (the PAN inside it is not double-counted); Aadhaar-like ids (Verhoeff-checked);
     - organisation names (suffix heuristic);
     - PII flag on email, phone, PAN and Aadhaar;
  7. keyword doc type (invoice, contract, resume, report, policy, other; English + Hindi/Gujarati cues);
  8. extractive summary;
  9. idempotent persist (reprocess replaces the derived rows), plus an audit event and job finish/fail.
- **Search**: `GET documents/search?q=&k=&document_ids=`. pgvector cosine top-20 and FTS top-20 (OR of query terms, `simple` config) are fused with reciprocal rank fusion (k=60). Each result carries `highlights` (term offsets).
- **Q&A**: `POST documents/ask {question, document_ids?}` retrieves the top 8 chunks and builds S1..Sn sources (6k-token budget, ordered by document and page). It then calls `GeminiService.generate_structured` with prompt `docs.answer` (new, declares DOCUMENT_TEXT only) and payload `DOCUMENT_TEXT: document:<id>...`, so the real policy gate decides.
  - Each citation quote is verified locally (rapidfuzz partial ratio ≥ 90).
  - If no citation is valid, the answer becomes "Not found in the provided documents." plus the passages.
  - If the gate blocks (or AI is not configured, out of quota, or returns invalid output), the response is an **extractive answer**: the best query-matching sentences, each cited, with `reason` + `message` and no network call. Blocked attempts are still logged as `blocked_by_policy`.
- **Summary**: `GET documents/{id}/summary?mode=extractive|generative`. Extractive is the default (local sentence scoring). `generative` uses `docs.summarize`, requires `documents.ask`, and falls back to extractive with a `reason`.
- **Registry**: `register_asset_classifier("document", ...)` and `register_asset_exists("document", ...)`. Unknown documents classify as `confidential`.
- **Isolation**: `apps/documents/tests/isolation.py` registers factories for every `document-*` route that has a `pk`.

## API shapes (new; CONTRACT.md has no documents section yet — please add)
All routes are under `/api/v1/orgs/{org}/`.

| Endpoint | Permission | Request / response |
|---|---|---|
| `GET documents` | `documents.view` | Filters: `?status=&doc_type=&classification=&q=`. Returns `{items:[Document], next_cursor, previous_cursor}`. |
| `POST documents` | `documents.manage` | Multipart: `file`, `title?`, `classification?`, `attest?`. Returns **202** `{document, job_id, status_url, events_url}`. Errors: 413 `upload_too_large`; 422 `upload_type_not_allowed`, `upload_content_mismatch`, `upload_zip_bomb`, `upload_macro_rejected`, `upload_xml_unsafe`, `upload_empty`, `attestation_required`; 503 `storage_unavailable`. |
| `GET documents/{id}` | `documents.view` | Document plus `languages`, `doc_type_scores`, `summary`. |
| `PATCH documents/{id}` | `documents.manage` | `{title}` |
| `DELETE documents/{id}` | `documents.manage` | 204 |
| `POST documents/{id}/classification` | `documents.manage` | `{classification, attest}` |
| `POST documents/{id}/reprocess` | `documents.manage` | 202 + job |
| `GET documents/{id}/pages` | `documents.view` | Cursor-paginated by number (default 20, `?limit=`). Item: `{id, number, text, method, ocr_confidence, char_count, language, label}`. |
| `GET documents/{id}/chunks` | `documents.view` | Cursor-paginated by ordinal. Item: `{id, ordinal, text, page, page_to, section, token_count, has_embedding}`. |
| `GET documents/{id}/entities` | `documents.view` | `?kind=&pii=true`. Returns `{items:[{id, kind, value, normalized, is_pii, confidence, page, chunk_id, start, end}], truncated}`. |
| `GET documents/{id}/tables` | `documents.view` | `{items:[{id, index, page, title, header, rows, n_rows, n_cols, truncated}]}` |
| `GET documents/{id}/summary` | `documents.view` (+ `documents.ask` for generative) | `{document_id, mode, summary, sentences:[{text, page}], key_points, reason, message, ai_request_ids}` |
| `GET documents/search` | `documents.view` | `{query, results:[{chunk_id, document_id, document_title, page, page_to, section, text, score, highlights:[[s,e]]}], retrieval:{vector, fulltext, fusion:"rrf"}}` |
| `POST documents/ask` | `documents.ask` | `{question, mode:"generative"\|"extractive", answer, citations:[{document_id, page, chunk_id, quote, document_title, source_id}], passages:[…as search…], confidence, unanswerable, reason, message, ai_request_ids, retrieval}` |

`reason` takes one of these values:
- `ai_policy_blocked`
- `ai_not_configured`
- `ai_quota_exhausted` / `ai_rate_limited`
- `ai_unavailable`
- `ai_invalid_output`
- `no_valid_citations`
- `no_relevant_passages`
- `null`

## Tests (real counts)
Run from `backend/` with `$env:PYTEST_DB_SUFFIX="ai_ml"; python -m pytest apps/ai/tests apps/nl2sql/tests apps/documents/tests --create-db -p no:cacheprovider`.

- **Unit tests (no DB): 683 passed, 0 failed.**

  | Suite | Passed | New this wave |
  |---|---|---|
  | `apps/ai/tests` | 429 | 12 (verify_gemini ×7, durable alias ×5) |
  | `apps/nl2sql/tests` | 169 | contract code vocabulary, every security code refused, semantics scope / `context_from_semantics` |
  | `apps/documents/tests` | 85 | all new |

  The documents unit tests cover:
  - extraction ×15 (pdf text/headings/tables, OCR path, `ocr_unavailable`, pypdf fallback, corrupt pdf, page cap, docx incl. Hindi, xlsx, csv, txt incl. Hindi+Gujarati, utf-16/cp1252);
  - upload safety ×18;
  - language, tokens, headings, chunking, entities and classification ×34;
  - RRF, sources, citations, extractive answer/summary, pipeline + FakeEmbedder, and policy-gated `ask` with a fake transport ×18.
- **DB tests: 38 written, NOT EXECUTED (infra unavailable).** Postgres :5433, Redis :6380 and MinIO :9000 were unreachable, and the run errors on DB connect (`OperationalError: connection timeout expired`).
  - `apps/ai/tests/test_db.py` (14), of which 4 are new:
    - `test_blocked_log_row_survives_rolled_back_request`, which is transactional and skips if the settings have no `durable` alias;
    - the 3 consent-ledger gate tests.
  - `apps/nl2sql/tests/test_cache_db.py` (7, unchanged).
  - `apps/documents/tests/test_api.py` (17): upload→process for txt, pdf, docx, xlsx and csv; scanned pdf without tesseract; corrupt pdf → failed; upload rejections; permissions and attestation; hybrid search incl. Hindi; ask on a real document → extractive with zero network calls; ask on a synthetic document → generative; summary modes; classification + registry; patch/reprocess/delete; cross-org 404; RLS.
- **Checks:**
  - `ruff check apps/ai apps/nl2sql apps/documents apps/ml`: all passed.
  - `manage.py check`: no issues.
  - `makemigrations --check`: no changes.
  - `scripts/check_conventions.py`: OK.
  - `check_ownership.py --agent ai_ml --base main`: OK.
- No test calls Gemini live, downloads an embedding model, or needs the tesseract binary.
- Migration `documents/0001_initial` has not been applied to a real DB yet. It needs the `vector` extension, which `infra/postgres/init` creates.

## Requests
- **[settings, Integrator]** `nexora/settings/test.py` redefines `DATABASES` without the `durable` alias. Add `"durable"` with `TEST: {"MIRROR": "default"}` and `CONN_MAX_AGE=0`, as in base. Until then:
  - tests write logs through `default`;
  - the transactional survival test skips.
- **[settings, Integrator]** Add `DOCUMENT_MAX_UPLOAD_BYTES` (default 50 MB) and `DOCUMENT_MAX_PAGES` (default 500) to `base.py` and `.env.example`. The code reads them with `getattr` defaults.
- **[contract, Integrator]** Add the documents API shapes above to CONTRACT.md (W5 Document Intelligence section).
- **[infra/ops]** For OCR, install `tesseract-ocr` + `tesseract-ocr-hin` + `tesseract-ocr-guj` in the worker image that runs the `ml` queue. Without them, scanned pages are stored as `ocr_unavailable` with a document warning.
- **[deps, optional]** Add `opencv-python-headless` if deskew preprocessing is wanted (§7.1). Today the preprocessing is PIL only: grayscale, autocontrast, threshold.
- **[backend/bi]** Pass `semantics=` (the W2 `DatasetSemantics`) to `nl2sql.services.propose/remember/store_verified` for version-scoped caching. `context_from_semantics()` builds the `QuestionContext`.
- **[frontend]** The Documents module can consume the endpoints above.
  - Job progress comes over the existing SSE `/jobs/{id}/events`.
  - `ask.reason/message` explain extractive fallbacks.
  - `search.results[].highlights` are character offsets.

## Open issues / known limitations
- None of the DB-backed code has run against Postgres yet: the durable alias, migrations, pgvector/FTS queries, RLS on the 5 new tables, and the endpoints. It is covered only by the 38 unexecuted tests.
- Structured extraction (`docs.extract`), document comparison, re-ranking, the e5 vs bge-m3 evaluation, and the Gemini VISION OCR fallback (§7.1/§7.9/§7.10) are not built in this wave.
- Chunk token counts are a sentencepiece-like estimate, not the real e5 tokenizer, which is not loaded at chunk time.
- DOCX page numbers rely on `lastRenderedPageBreak` / explicit breaks, so they are approximate for documents never rendered by Word.
- `apps/ml` is untouched (not in W2 scope).

## Defects for other agents
- **[qa_security]** `tests/isolation/test_static_tenancy.py::test_tenant_unique_constraints_include_org` still fails on `accounts.ApiKey.prefix` / `Invitation.token_hash` (the known W1 allowlist issue). It is not caused by this branch; the new documents constraints all include `org`.
- **[backend]** `apps.core.storage.download_to/put_object` require an active tenant ContextVar (`_check_key`). A long-running task therefore has to do its download inside a `tenant_step` transaction. A helper that sets only the ContextVar, without a transaction, would let Celery bodies avoid holding a DB transaction during I/O.
