# Security Architecture

> Status: **Design.** Last updated 2026-11-17. Related: `MULTI_TENANCY_DESIGN.md`, `AI_ARCHITECTURE.md` §5, `TESTING_STRATEGY.md` §4.
> Security-critical modules: `core.tenancy`, `sqlguard`, `ai.policy`, `core.crypto`, `connectors` (SSRF). Each gets an **independent QA/Security review** before it can be marked Working.

## 1. Assets and trust boundaries

| Asset | Sensitivity | Where it lives |
|---|---|---|
| Tenant business data (ingested datasets) | High | `nexora_wh` org schemas, MinIO `orgs/{id}/uploads` |
| Customer external DB credentials | Critical | `credential` (envelope-encrypted) |
| User credentials and sessions | Critical | `user.password` (argon2id), `django_session` |
| API keys | High | `api_key.key_hash` (hash only) |
| Audit logs and query audit | High (integrity) | Append-only, hash-chained tables |
| Gemini API key | Critical | Env / secret store only |
| Model artifacts | Medium | MinIO `orgs/{id}/ml/` |
| Documents | High | MinIO + `document_page`/`document_chunk` |

**Trust boundaries:**
1. Browser ↔ Caddy (TLS).
2. API ↔ Postgres/Redis/MinIO (internal network).
3. Workers ↔ customer external DBs (egress).
4. Workers ↔ Google Gemini (egress, data-policy gate).
5. Tenant ↔ tenant (logical: RLS plus schema privileges).
6. Platform admin ↔ tenants (BYPASSRLS ops alias, restricted endpoints).

## 2. Threat model (STRIDE summary)

| Threat | Example | Primary controls |
|---|---|---|
| **Spoofing** | Credential stuffing, stolen session, forged API key | argon2id, login rate limits + progressive lockout, TOTP MFA (required for owner/admin in production), `Secure`/`HttpOnly`/`SameSite` cookies, session rotation on login, API keys hashed with a visible prefix |
| **Tampering** | Editing audit logs, mutating dataset versions, prompt injection that alters actions | Append-only triggers + hash chain; immutable warehouse tables; validated structured outputs; SQL guard; agent tool allowlist + approvals |
| **Repudiation** | "I never ran that query" | `bi_query_audit`, `audit_event`, `access_history` with request ID, IP and actor |
| **Information disclosure** | Cross-tenant reads, AI-generated SQL reading forbidden columns, data sent to the Gemini free tier, SSRF to internal services | RLS + fail-closed managers; schema-per-org warehouse roles; secure-source rewrite + column allowlist; `AIDataPolicy` gate (default `synthetic_only`); SSRF IP pinning; 404-not-403 |
| **Denial of service** | Huge uploads, cartesian-join SQL, recursive CTEs, quota draining | Upload caps + zip-bomb checks; guard (no recursive CTEs, plan cost cap, statement timeout, row and byte caps); per-org AI budgets; rate limits; worker memory limits |
| **Elevation of privilege** | Analyst edits policies; agent modifies users; SQL `SET ROLE`/`set_config` | Permission codenames per endpoint; agents cannot call governance tools; function allowlist forbids `set_config`, `current_setting` and the `*_role` functions; per-org DB roles cannot `SET ROLE` |

## 3. Authentication
- **Passwords:**
  - argon2id (Django `Argon2PasswordHasher`), minimum 12 characters, zxcvbn strength check, breached-password check against a **local** list (no external API).
  - Reset tokens are single-use, expire after 30 minutes and are stored hashed.
- **Sessions:**
  - Server-side sessions in the DB (cached in Redis), with a 12 h idle timeout and a 7-day absolute maximum.
  - The session ID rotates on login and on privilege change.
  - Cookie: `Secure; HttpOnly; SameSite=Lax`. CSRF via Django middleware plus the `X-CSRFToken` header.
- **MFA:** TOTP (django-otp). Required for owner/admin and platform admins in production; optional for others in the MVP.
- **Re-authentication** (≤ 5 min freshness) is required for:
  - AI data-policy consent;
  - API key creation;
  - role and permission changes;
  - credential edits;
  - approving high-impact actions.
- **API keys:** `nxk_<prefix>_<secret>`, 256-bit secret, stored as HMAC-SHA256 with a server pepper. Scoped, expiring and revocable, bound to one org. `last_used_at` is tracked.
- **Login protection:** 5 failures in 15 min per account+IP triggers exponential backoff. Responses are generic ("invalid email or password").
- **SSO (OIDC/SAML)** is planned post-MVP.

## 4. Authorization
- **Layered checks for every tenant request:**
  1. org membership (`nexora_membership_check`);
  2. role permission codename;
  3. resource ACL (datasets);
  4. data policies (column and row) applied inside the SQL guard;
  5. Postgres RLS as a safety net.
- **Policy model:** Deny wins. A column `deny` overrides any allow. Masks combine to the most restrictive. Row policies are ANDed across all applicable policies (user-specific + role + everyone), unless the role is in `exempt_role_ids`.
- **Effective policy** is computed by `governance.effective_policy(user, dataset)`. The result is a frozen `QueryPolicy` with a `fingerprint` (a hash of all inputs), recorded in every query audit.
- **Owner safeguards:** the last owner cannot be removed or demoted. Owner-only actions are org deletion and AI policy consent (admins can also consent).

## 5. SQL guard (`sqlguard`): defence in depth for AI-generated and manual SQL

**Invariant:** *No SQL reaches a database unless it was parsed, validated, rewritten and regenerated by the guard. Raw model output is never executed.*

### 5.1 Validation pipeline
| # | Step | Reject codes |
|---|---|---|
| 1 | Size limits: ≤ 10 KB of text, ≤ 1,500 AST nodes, nesting depth ≤ 12 | `too_large` |
| 2 | `sqlglot.parse(sql, read="postgres")`: **exactly one** statement; `exp.Command` (sqlglot's fallback for `COPY`, `DO`, `SET`, `CALL`…) rejected | `parse_error`, `multiple_statements`, `command_not_allowed` |
| 3 | Root must be `Select` or a set operation (`Union`/`Intersect`/`Except`) of Selects | `not_select` |
| 4 | Node blocklist anywhere in the tree: `Insert`, `Update`, `Delete`, `Merge`, `Create`, `Drop`, `Alter`, `Into` (SELECT INTO), `Lock` (FOR UPDATE/SHARE), `Set`, `Transaction`, `Copy`, `Placeholder`/`Parameter` in model output, recursive `With` | `forbidden_construct` |
| 5 | **Function allowlist** (≈ 80): aggregates (`sum avg min max count`, `count(distinct)`, `percentile_cont`, `stddev`, `variance`, `string_agg`, `array_agg`), window functions (`row_number rank dense_rank lag lead ntile first_value last_value`), dates (`date_trunc extract date_part now current_date age make_date to_char to_date`), conditionals (`coalesce nullif greatest least case`), strings (`lower upper trim length substring position replace concat left right split_part`), math (`round floor ceil abs power sqrt ln log mod`). Checked on `exp.Func` subclasses **and** `exp.Anonymous` names. Rejected examples: `pg_sleep`, `dblink*`, `query_to_xml*`, `current_setting`, `set_config`, `lo_*`, `pg_read_*`, `pg_ls_dir`, `generate_series`, `repeat`, `txid_*`, `version`, `inet_server_addr` | `forbidden_function` |
| 6 | No set-returning or table functions in `FROM`; no `LATERAL` function calls | `forbidden_function` |
| 7 | **Tables:** every non-CTE `exp.Table` must be in the policy's allowed set. Rejected: catalogs or databases (3-part names), any schema other than the tenant's own, `pg_*`, `information_schema`, CTE names shadowing real tables | `table_not_allowed`, `system_catalog`, `cte_shadowing` |
| 8 | Casts only to an allowlisted type set; `reg*` types, `pg_*` types and system columns (`ctid xmin xmax cmin cmax tableoid oid`) rejected | `forbidden_type`, `system_column` |
| 9 | **Columns:** `qualify(ast, schema=<allowed columns only>, expand_stars=True, validate_qualify_columns=True, quote_identifiers=True)`. A disallowed column fails resolution. `*`/`t.*` expand to allowed columns only. Whole-row references (`SELECT t FROM x t`, `row_to_json(t)`, `to_jsonb(t)`) are rejected. `NATURAL JOIN` is rejected. | `column_not_allowed`, `whole_row_reference` |
| 10 | **Secure-source substitution:** in every scope (CTEs, subqueries, set-op branches, lateral), each base table becomes `(SELECT <allowed cols with mask exprs> FROM org_x.ds_N_vM WHERE <row predicates>) AS <alias>`. Predicates are built with sqlglot builders and bound parameters (`%(p1)s`), never string concatenation. | — |
| 11 | Wrap: `SELECT * FROM (<q>) AS _q LIMIT <row_cap+1>` (to detect truncation) | — |
| 12 | Regenerate with `comments=False`. **Round-trip check**: `parse(generate(ast)) == ast`. | `roundtrip_mismatch` |
| 13 | **Plan check** in the execution transaction: `EXPLAIN (FORMAT JSON, VERBOSE) <sql>`. Every `Relation Name`/`Schema` must be in the allowed set, and `Total Cost` must be ≤ `NL2SQL_MAX_COST` (default 5e6). | `plan_relation_not_allowed`, `cost_exceeded` |

**Masking happens at the source**, so a masked column stays masked in `WHERE`, `ORDER BY`, `GROUP BY`, joins and aggregates:

| Mask | Expression |
|---|---|
| `mask_null` | `NULL::<type>` |
| `mask_hash` | `encode(sha256(convert_to(col::text || <org salt param>,'UTF8')),'hex')` (an allowlisted internal function, inserted only by the rewriter) |
| `mask_partial` | `repeat('*', greatest(length(col)-4,0)) || right(col,4)` (inserted by the rewriter; users cannot call `repeat` themselves) |

Functions inserted by the rewriter are tagged on the AST, so the allowlist check does not run on them. The allowlist runs **before** the rewrite.

### 5.2 Execution
- **Connection:**
  - psycopg 3, not the ORM.
  - Ingested data: connect as `wh_q_<org>`.
  - External DB: connect as the customer's verified `nexora_ro` user, pinned IP, `sslmode=verify-full`.
- **Per query:**
  1. `BEGIN READ ONLY`
  2. `SET LOCAL statement_timeout = '15s'; SET LOCAL lock_timeout = '1s'; SET LOCAL idle_in_transaction_session_timeout = '20s'; SET LOCAL work_mem = '32MB'; SET LOCAL search_path = org_<short_id>` (external: `search_path` = the allowlisted schema)
  3. EXPLAIN check (§5.1 #13)
  4. Server-side named cursor, `fetchmany(500)`, stopping at `row_cap` (default 1,000; org-configurable ≤ 5,000) or `byte_cap` (5 MB). Cells longer than 2 KB are truncated.
  5. `ROLLBACK` (always; never commit)
- **Execution queues:** `query_wh` for ingested data and `query_ext` for external DBs. In production, `query_ext` workers have egress only to the internet, not to internal networks.

### 5.3 Manual SQL (`/bi/queries`)
The same pipeline applies. Only the `bi.sql` permission differs. Nothing bypasses it, not even an org owner.

### 5.4 Audit
Every validation, whether allowed or rejected, writes `bi_query_audit` with `raw_sql`, `executed_sql`, params, verdict, reject code, policy fingerprint, exec role, plan cost, duration, rows and bytes.

## 6. Secrets and encryption
| Item | Protection |
|---|---|
| Transport | TLS 1.2+ at Caddy (HSTS); internal Postgres with TLS in production; MinIO over HTTPS in production |
| At rest (DB) | Disk or volume encryption (cloud provider) in production |
| Credentials (`credential` table) | **Envelope encryption.** A per-record 256-bit DEK, AES-256-GCM with AAD `org:<id>:credential:<id>`. The DEK is wrapped by a KEK from `NEXORA_KEK` (env, base64 32 bytes) or a cloud KMS later. `kek_id` supports rotation; a re-wrap job runs on rotation. |
| Gemini API key | `GEMINI_API_KEY` env/secret store only; never in the DB in the MVP (BYO keys later are stored as credentials) |
| Django `SECRET_KEY`, API key pepper | Env/secret store; rotated at deployment |
| Object storage | Server-side encryption (SSE-S3) in production; pre-signed URLs with 10-minute expiry |
| Logs | structlog processors drop or mask keys matching `password`, `secret`, `token`, `authorization`, `api_key`, `credential`, `cookie` |

`.env` files are git-ignored. `.env.example` holds only placeholder values. A secret scanner (gitleaks) runs in CI and as a pre-commit hook.

## 7. Upload and file security
- Streaming upload to MinIO with a max size. Content type is checked by magic bytes, not by extension.
- **Zip-bomb protection** for xlsx/docx: inspect the zip central directory and reject if the uncompressed total exceeds 1 GB or the ratio exceeds 100.
- `defusedxml` for all XML parsing. openpyxl runs in read-only mode, and formulas are never evaluated.
- **PDF:** pdfplumber/pypdf with page-count (≤ 2,000) and per-page time limits. No JavaScript execution.
- **Export formula injection:** cells beginning with `= + - @ \t \r` are prefixed with `'` in CSV/XLSX exports.
- Antivirus scanning (ClamAV container) is optional in production. It is flagged as recommended for real customer files.

## 8. Network security
- **SSRF guard** (connectors, agent `http_request`, REST connector):
  - DNS resolution happens once, and the IP is checked against deny ranges: loopback, RFC 1918, link-local incl. 169.254.169.254, CGNAT, ULA, multicast, and platform subnets.
  - The connection goes to the pinned IP. Redirects are re-checked, with a maximum of 3.
- **Production network zones:** containers sit on internal networks. Only Caddy is published. Postgres, Redis and MinIO are never exposed.
- **Security headers** (Caddy/Next.js):
  - `Content-Security-Policy` (self, no inline scripts except Next.js nonce, `connect-src 'self'`);
  - `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`, `Permissions-Policy` minimal;
  - `Strict-Transport-Security`.
- **CORS:** none needed, because everything is same-origin. CORS is disabled.

## 9. Audit, monitoring and incident response
- **Logs:**
  - `audit_event`: mutations, auth events, policy changes, consents, exports.
  - `bi_query_audit`: every query.
  - `access_history`: views, queries, exports, downloads, predictions.
  - `ai_request_log`: every AI call or block.
  - `platform_audit_event`: super-admin actions.
- **Integrity:** per-org hash chains, sealed every minute. `/audit-events/verify` and a nightly verification job alert on any break.
- **Alerts** (Prometheus/Grafana → email):
  - spikes in `sql_rejected` with security codes;
  - failed logins;
  - `tenant_context_missing` (should be 0);
  - RLS zero-row anomalies in tests;
  - Gemini breaker open;
  - an audit chain break.
- **Incident runbook** (`DEPLOYMENT_ARCHITECTURE.md` §8): contain (suspend the org or revoke keys), preserve the audit evidence, rotate secrets (KEK re-wrap, `SECRET_KEY`, Gemini key), notify.

## 10. OWASP Top 10 (2021) mapping
| OWASP | Nexora controls |
|---|---|
| A01 Broken access control | Membership + permission + ACL + data policies + RLS; 404-not-403; isolation test suite on every endpoint |
| A02 Cryptographic failures | TLS, argon2id, envelope encryption, hashed tokens and keys |
| A03 Injection | SQL guard (parse + rewrite + regenerate), ORM parameterization, no raw string SQL elsewhere (lint rule), prompt-injection hygiene, formula-injection escaping |
| A04 Insecure design | Threat model, default-deny AI policy (`synthetic_only`), human approval for high-impact actions |
| A05 Security misconfiguration | Settings split, prod system checks (`DEBUG=False`, secure cookies, `ALLOWED_HOSTS`), minimal exposed ports, CSP |
| A06 Vulnerable components | `pip-audit`, `npm audit`, Dependabot/Renovate, Trivy image scan in CI |
| A07 Identification and authentication failures | MFA, lockout, session rotation, re-authentication for sensitive actions |
| A08 Software and data integrity failures | Lockfiles with hashes (`uv.lock`, `package-lock.json`), signed images (later), audit hash chain, model artifact sha256 |
| A09 Security logging and monitoring failures | Comprehensive audit + alerts |
| A10 SSRF | SSRF guard with IP pinning, isolated `query_ext` egress |

## 11. LLM-specific risks (OWASP LLM Top 10 alignment)
| Risk | Control |
|---|---|
| Prompt injection (direct or via data or documents) | Data blocks delimited; outputs validated; SQL guarded; agents' tools allowlisted; high-impact actions need approval |
| Sensitive information disclosure | `AIDataPolicy` gate (default `synthetic_only`), redaction, PII aliasing, column exclusion |
| Insecure output handling | No output executed without validation; Markdown rendered with sanitization (no raw HTML) |
| Excessive agency | Tool allowlists, scopes, step limits, approvals, no governance tools |
| Overreliance and hallucination | SQL transparency, citation verification, "Not found" behaviour, confidence labels |
| Model denial of service and cost | Quotas, budgets, breaker, output token caps |
| Supply chain | Only the official `google-genai` SDK; lockfiles; import-linter rule |

## 12. Security acceptance gates (must pass before a module is "Working")
1. Tenant isolation suite: 0 failures (generated per endpoint from the URLconf, two orgs, every method).
2. SQL attack corpus (`TESTING_STRATEGY.md` §4.2): 100% rejected or neutralized; Hypothesis fuzzing finds no accepted forbidden constructs in 10k examples.
3. AI policy tests: no network call for blocked payloads; default `synthetic_only` on all org-creation paths; `schema_only` requires consent.
4. No secrets in logs (log-capture test with known secret values).
5. `pip-audit`, `npm audit` (high/critical), Trivy (critical) and gitleaks are all clean.
6. An independent QA/Security review sign-off is recorded in `coordination/handoffs/` for `core.tenancy`, `sqlguard`, `ai.policy`, `core.crypto` and `connectors` SSRF.
