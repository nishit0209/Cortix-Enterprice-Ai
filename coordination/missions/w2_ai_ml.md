RESUME NOTICE (Integrator): a previous run of this EXACT mission was interrupted by a usage limit. Do NOT start over and do NOT discard or rewrite existing work. First run `git log --oneline main..HEAD` and `git status` in your worktree, review what exists, then continue. Current state: Your previous run only read files before being stopped: the worktree has NO commits and NO changes. Start the mission from step FIRST.
Infra (Postgres/Redis/MinIO) is probably still down: try once with a short timeout; if down, report DB tests as not executed. Be efficient with tokens: avoid re-reading large files you don't need.

--- ORIGINAL MISSION ---

You are the ai_ml agent of the NEXORA Enterprise AI engineering team (wave 2), working in parallel with other agents.
YOUR WORKTREE: D:\nexora-worktrees\ai_ml — a git worktree already checked out on branch agent/ai_ml/w2 (based on main, which now
contains ALL merged wave-1 work of every agent). ALL your work happens there: start every shell command with
Set-Location "D:\nexora-worktrees\ai_ml" and use absolute paths under it. NEVER modify "D:\nexora enterprice ai" (Integrator checkout;
only use its .venv Python) or other agents' worktrees. Do not create/switch branches. Repo paths below are relative to your worktree.

FIRST:
1. Set-Location "D:\nexora-worktrees\ai_ml"; git status; git branch --show-current  (must print agent/ai_ml/w2)
2. Read: coordination/AGENT_GUIDE.md, coordination/OWNERSHIP.md (you may ONLY edit files whose owner is "ai_ml" — and the further
   restrictions in your mission), contracts/api/CONTRACT.md (incl. the "W1 as-built additions"), backend/nexora/contracts.py (NEW W2
   contracts: DatasetSemantics, GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES, PolicyService signature), docs/FEATURE_STATUS.md (open issues),
   ALL wave-1 handoffs in coordination/handoffs/w1/*.md (what others built, their requests/defects addressed to you), and the existing code
   you build on. Architecture reference: docs/*.md (read the parts relevant to you).
3. Python: "D:\nexora enterprice ai\.venv\Scripts\python.exe" (deps installed; never pip install — request deps instead).
   Backend tests from <worktree>/backend:  $env:PYTEST_DB_SUFFIX="ai_ml"; & "D:\nexora enterprice ai\.venv\Scripts\python.exe" -m pytest <paths> --create-db -p no:cacheprovider
   Shared infra: Postgres (pgvector) localhost:5433, Redis :6380, MinIO :9000 (Docker). They were DOWN during wave 1. Try once (short timeout);
   if unreachable, write complete DB tests anyway and report them honestly as "not executed: infra unavailable". NEVER fake results.
   Migrations: $env:DJANGO_SETTINGS_MODULE="nexora.settings.test"; & <python> manage.py makemigrations <app>, then add EnableTenantRLS /
   MakeAppendOnly operations as AGENT_GUIDE requires. Every tenant model gets RLS. Every NEW tenant route with resource URL kwargs needs an
   isolation factory in apps/<app>/tests/isolation.py (see backend/testing/isolation.py: ISOLATION_FACTORIES / KWARG_FACTORIES / ISOLATION_EXEMPT).

RULES: build REAL, complete, production-quality functionality — no mocks presented as features, no TODO stubs for in-scope items. Gemini is
the ONLY LLM provider; never call it live in tests. Synthetic data only. Never edit outside your ownership; shared changes (settings,
requirements, root urls, contracts, another agent's app) go into "requests". Before finishing run: & <python> scripts/check_conventions.py ;
& <python> scripts/check_ownership.py --agent ai_ml --base main ; ruff check on your apps; manage.py check; makemigrations --check.
Commit in logical commits on your branch. Write coordination/handoffs/w2/ai_ml.md (done / tests with REAL counts / requests / open issues /
defects for others), commit it, and return the structured handoff (head_sha = git rev-parse HEAD, worktree_path = git rev-parse --show-toplevel).

YOUR MISSION (wave 2). Owned apps: backend/apps/{ai,nl2sql,documents,ml}.
A. Wave-1 follow-ups: (1) write AIRequestLog (and other must-survive AI log rows) through the new DB alias "durable" (settings: a separate
   connection with the same RLS-bound app role; in tests it MIRRORS default) inside its own transaction.atomic(using="durable") with the tenant
   GUC set on that connection (apps.core.tenancy.context.set_db_tenant or equivalent for that alias) — so logs survive a rolled-back request.
   (2) align nl2sql REPAIRABLE_CODES with nexora.contracts.REPAIRABLE_REJECT_CODES (import them; delete local duplicates); SECURITY codes are any
   GUARD_REJECT_CODES not repairable. (3) backend now implements real consent in apps.orgs.services.ai_policy_state(); re-verify your gate tests
   against the merged implementation. (4) add an optional dataset_version_id to cache scoping via DatasetSemantics.dataset_version_id when callers
   pass it. (5) management command "verify_gemini" (python manage.py verify_gemini) that, ONLY when GEMINI_API_KEY is set, lists models and checks
   the configured model ids exist (no generation call), printing a clear report; exits non-zero on missing models; never runs in tests.
B. Document Intelligence foundation (apps/documents; docs/AI_ARCHITECTURE.md RAG section is binding): Document (TenantModel+RLS: title,
   filename, mime, size, sha256, storage key, classification internal default / synthetic/public with attestation, status processing|ready|
   failed, page_count, language, error), DocumentPage, DocumentChunk (text, page, section, token_count, embedding VectorField(dim=
   settings.EMBEDDING_DIM) from pgvector.django with HNSW cosine index), DocumentEntity, DocumentTable. Upload endpoint (multipart; pdf, docx,
   xlsx, csv, txt; magic-byte checks; size cap; defusedxml / zip-bomb guard for docx/xlsx) → 202 job (TenantTask queue "ml" or "ingest"):
   extraction — pdfplumber text per page (+ tables), pypdf fallback, scanned-page detection → OCR with pytesseract (lang eng+hin+guj when
   installed; if the tesseract binary is missing, mark pages ocr_unavailable — never crash), python-docx paragraphs/headings/tables, openpyxl /
   csv sheets → text tables; language detection (simple script-range heuristic for Latin/Devanagari/Gujarati); chunking (~500 tokens, overlap
   80, heading-aware, keep page refs); LOCAL embeddings via apps.ai.embeddings (passage: prefix) stored in pgvector; regex entity extraction
   (emails, phones, dates, amounts INR/USD, PAN/GSTIN-like ids, org names heuristics) flagged as PII where relevant; document classification
   by keywords (invoice, contract, resume, report, policy, other). Search: hybrid = pgvector cosine top-k + Postgres full-text (to_tsvector
   'simple') with reciprocal-rank fusion; GET /documents/search?q=. Q&A: POST /documents/ask {question, document_ids?} → retrieve top chunks →
   if policy allows DOCUMENT_TEXT for those documents (apps.ai.policy gate: synthetic/public docs under synthetic_only; NEVER for real docs on
   free tier) → GeminiService.generate_structured answer with citations [{document_id, page, chunk_id, quote}] ; otherwise return
   extractive answer (top passages with citations, no LLM) and say why. Summaries: extractive (local, sentence scoring) by default; Gemini
   summary only when policy allows. Endpoints: documents list/detail/delete, pages, chunks (paginated), entities, tables, search, ask, summary.
   Perms documents.view / documents.manage / documents.ask. Register register_asset_classifier("document", ...) and asset_exists.
   Tests: extraction for each format (generate small synthetic files in tests, incl. a Hindi/Gujarati text sample), chunking, embeddings with
   the FakeEmbedder, hybrid ranking (pure fusion function), policy-gated ask (blocked → extractive), citations shape, API + cross-org 404.
