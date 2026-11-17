# API Specification

> Status: **Design.** Last updated 2026-11-17.
> - The machine-readable contract will be `contracts/openapi.yaml`, owned by the Integrator. This document is the human-readable source for it.
> - CI generates the drf-spectacular schema and diffs it against the contract (oasdiff); any drift fails the build.
> - SSE payload schemas will live in `contracts/events/*.schema.json`.

## 1. Conventions

| Topic | Rule |
|---|---|
| Base path | `/api/v1`. Tenant resources live under `/api/v1/orgs/{org}/…`, where `{org}` is the org slug; platform resources under `/api/v1/platform/…`. |
| Format | JSON (`application/json; charset=utf-8`). snake_case fields, ISO-8601 UTC timestamps, UUID strings. |
| Authentication | **Browser:** Django session cookie (`HttpOnly; Secure; SameSite=Lax`) plus a CSRF header (`X-CSRFToken`) on unsafe methods, same-origin through Caddy. **Programmatic:** `Authorization: Bearer nxk_<prefix>_<secret>` API keys, scoped. API keys cannot reach `auth/*`, `members`, `roles` or governance-edit endpoints. |
| Tenant resolution | From `{org}` in the path, plus an active membership check (`nexora_membership_check`). An API key is bound to exactly one org, and a mismatch returns 404. |
| Authorization | Permission codenames (`DATABASE_SCHEMA.md` §3) checked per endpoint, plus the dataset ACL where relevant. **Unauthorized tenant resources return 404, not 403**, so their existence is not revealed. |
| Pagination | Cursor-based: `?cursor=<opaque>&limit=<1..200, default 50>`. Response: `{ "items": [...], "next_cursor": "..."|null }`. |
| Filtering and sorting | `?status=failed&created_after=…&ordering=-created_at` (django-filter allowlists per endpoint). |
| Idempotency | `Idempotency-Key` header (UUID) required on POSTs that start jobs. A repeat within 24 h returns the original `202` body. |
| Async jobs | Long-running operations return `202 Accepted` with `{ "job_id": "...", "status_url": "...", "events_url": "..." }`. |
| Errors | RFC 7807 `application/problem+json`: `{ "type", "title", "status", "code", "detail", "request_id", "errors": [{ "field", "code", "message" }] }`. |
| Rate limits | Per user and per API key (Redis token bucket). Exceeding the limit returns `429` with `Retry-After`. Gemini quota exhaustion is a separate code (§3). |
| Request IDs | Every response carries `X-Request-ID`. A client-supplied value is accepted if it is a valid UUID. |
| Versioning | Breaking changes go to `/api/v2`. Additive changes are allowed within v1 if the contract is updated first. |

## 2. Status codes

| Code | Use |
|---|---|
| 200 / 201 / 204 | Success |
| 202 | Job accepted |
| 400 | Validation error (`code` per field) |
| 401 | Not authenticated |
| 403 | Authenticated, member of the org, but missing the permission for a resource whose existence is not secret (for example the org settings page) |
| 404 | Not found, **or not visible to you** (tenant or ACL) |
| 409 | Conflict (duplicate name, stale version) |
| 413 | Upload too large |
| 415 | Unsupported file type |
| 422 | Semantically invalid (for example a pipeline DAG cycle, or SQL rejected by the guard) |
| 429 | Rate limited (`code: rate_limited`) or AI quota exhausted (`code: ai_quota_exhausted`, includes `resets_at`) |
| 503 | Dependency unavailable (`code: ai_unavailable`, `warehouse_unavailable`) |

## 3. Stable error codes (excerpt)

| Code | Meaning |
|---|---|
| `tenant_context_missing` | Internal guard; should never reach clients (500 plus an alert) |
| `ai_policy_blocked` | The org's `AIDataPolicy` forbids sending this payload class or asset to Gemini |
| `ai_policy_consent_required` | `schema_only` (or higher) needs a valid org-admin consent |
| `ai_policy_tier_forbidden` | `aggregates_only`/`full` while `GEMINI_TIER=free` |
| `ai_quota_exhausted` | Project or org daily Gemini budget reached; `resets_at` is given |
| `ai_invalid_output` | Gemini output failed schema validation after one repair attempt |
| `sql_rejected` | The guard rejected the SQL; `detail` holds the reject code (`multiple_statements`, `forbidden_function`, `table_not_allowed`, `column_not_allowed`, `system_catalog`, `plan_relation_not_allowed`, `cost_exceeded`, …) |
| `query_timeout` | `statement_timeout` hit |
| `result_truncated` | Informational flag on results that hit the row or byte cap (not an error) |
| `connector_not_readonly` | The external DB role failed the read-only privilege check |
| `connector_host_forbidden` | SSRF guard: the host resolved to a private, loopback or link-local address |
| `upload_rejected` | Size, type, zip-bomb ratio or parse failure |

## 4. SSE event contract

- **Content type:** `text/event-stream`. Each event has `id:` (the Redis Stream ID), `event:` (the type) and `data:` (JSON).
- **Resuming:** reconnect with the `Last-Event-ID` header.
- **Heartbeat:** a `:hb` comment every 15 s. Streams close after the `done` or `error` event, or after a maximum lifetime of 10 minutes.

### 4.1 Job events: `GET /orgs/{org}/jobs/{job_id}/events`
| event | data |
|---|---|
| `status` | `{ "status": "queued|running|succeeded|failed|cancelled", "progress": 0-100, "message": "..." }` |
| `log` | `{ "level": "info|warning|error", "message": "...", "step_key": "..." }` |
| `result` | `{ "result_ref": { ... } }` |
| `done` | `{ "status": "succeeded|failed|cancelled" }` |
| `error` | problem+json body |

### 4.2 Chat events: `POST /orgs/{org}/bi/conversations/{id}/messages` (`Accept: text/event-stream`)
| event | data |
|---|---|
| `status` | `{ "stage": "authorizing|resolving|generating|validating|executing|explaining", "source": "cache_exact|cache_semantic|builder|llm" }` |
| `sql` | `{ "raw_sql": "...", "executed_sql": "...", "assumptions": [..], "tables": [..], "query_audit_id": "..." }` |
| `result` | `{ "columns": [{ "name", "type", "masked": bool }], "rows": [[...]] (≤ 200 inline), "row_count", "truncated", "result_ref", "chart_spec": { "type": "bar|line|area|pie|scatter|table|kpi", "x", "y", "series" } }` |
| `insight` | `{ "kind": "top_n|period_change|anomaly|trend", "text": "...", "data": {...} }` (local) |
| `token` | `{ "text": "..." }` (Gemini narration chunks, only when the policy allows values for this asset) |
| `done` | `{ "message_id": "...", "ai_requests_used": 0-2 }` |
| `error` | problem+json (`ai_policy_blocked`, `ai_quota_exhausted`, `sql_rejected`, `query_timeout`, …) |

## 5. Endpoints by phase

Legend: **Perm** is the required permission codename. `ACL` means a dataset ACL privilege is also required.

### Phase 2: Authentication, orgs and RBAC
| Method | Path | Perm | Notes |
|---|---|---|---|
| POST | `/auth/signup` | public | Creates user + org (`ai_data_policy` forced to `synthetic_only`; the request body cannot set it) + owner membership + seeded roles. Rate-limited. |
| POST | `/auth/login` | public | Session login; MFA challenge if enabled. Rate-limited with lockout backoff. |
| POST | `/auth/logout` | auth | |
| POST | `/auth/mfa/totp/setup` · `/verify` · `/disable` | auth | |
| POST | `/auth/password/reset-request` · `/reset-confirm` | public | Generic response (no account enumeration) |
| GET | `/me` | auth | User profile and orgs (via `nexora_user_orgs`) |
| GET, PATCH | `/orgs/{org}` | `org.view` / `org.settings.edit` | `ai_data_policy` is **not** writable here (see below) |
| GET | `/orgs/{org}/ai-policy` | `org.view` | Current policy, consent status, Gemini tier, which features are enabled |
| POST | `/orgs/{org}/ai-policy/consents` | `org.ai_policy.consent` (owner/admin) | Body `{ "policy": "schema_only", "consent_text_version": "...", "acknowledged": true }`. Requires recent re-authentication (≤ 5 min). Rejects `aggregates_only`/`full` on the free tier. |
| POST | `/orgs/{org}/ai-policy/revoke` | `org.ai_policy.consent` | Immediately reverts to `synthetic_only` |
| GET | `/orgs/{org}/ai-policy/consent-text` | `org.view` | Versioned consent text |
| GET, POST | `/orgs/{org}/members` | `members.view` / `members.manage` | |
| PATCH, DELETE | `/orgs/{org}/members/{id}` | `members.manage` | Cannot demote or remove the last owner |
| GET, POST | `/orgs/{org}/invitations` | `members.manage` | Sends email (Mailpit in dev) |
| DELETE | `/orgs/{org}/invitations/{id}` | `members.manage` | Revoke |
| POST | `/invitations/accept` | public + token | `{ token, password?|session }` |
| GET, POST | `/orgs/{org}/roles` | `roles.view` / `roles.manage` | Custom roles |
| GET, PATCH, DELETE | `/orgs/{org}/roles/{id}` | `roles.manage` | System roles are immutable |
| GET | `/permissions` | auth | Permission catalog |
| GET, POST | `/orgs/{org}/api-keys` | `api_keys.manage` | The secret is shown once on create |
| DELETE | `/orgs/{org}/api-keys/{id}` | `api_keys.manage` | Revoke |
| GET | `/orgs/{org}/audit-events` | `audit.view` | Filters: actor, action, target, time range |
| GET | `/orgs/{org}/audit-events/verify` | `audit.view` | Hash-chain verification result |
| GET | `/orgs/{org}/jobs/{id}` · `/events` | owner of job or `jobs.view` | |

### Phase 3: Data Engineering Hub
| Method | Path | Perm | Notes |
|---|---|---|---|
| POST | `/orgs/{org}/uploads` | `datasets.create` | Multipart, streamed to MinIO. Size, type and zip-bomb checks. Returns the upload. |
| GET, POST | `/orgs/{org}/data-sources` | `sources.view` / `sources.manage` | The credential is write-only (`password` accepted, never returned) |
| GET, PATCH, DELETE | `/orgs/{org}/data-sources/{id}` | `sources.manage` | |
| POST | `/orgs/{org}/data-sources/{id}/test` | `sources.manage` | 202 job: connectivity + SSRF pin + **read-only privilege check** |
| POST | `/orgs/{org}/data-sources/{id}/introspect` | `sources.manage` | 202 job: schemas, tables, columns (within the allowlist) |
| GET, POST | `/orgs/{org}/datasets` | `datasets.view` / `datasets.create` | Create from upload, data source table or pipeline output |
| GET, PATCH, DELETE | `/orgs/{org}/datasets/{id}` | ACL read / `datasets.manage` | |
| POST | `/orgs/{org}/datasets/{id}/classification` | `datasets.classify` (owner/admin) | `{ classification, attest: true }`; `synthetic`/`public` require attestation; audited |
| GET | `/orgs/{org}/datasets/{id}/versions` · `/{v}` | ACL read | |
| GET | `/orgs/{org}/datasets/{id}/versions/{v}/columns` | ACL read | Stats; PII top values suppressed |
| GET | `/orgs/{org}/datasets/{id}/preview?limit=100` | ACL read | Column and row policies applied (same guard path) |
| POST | `/orgs/{org}/datasets/{id}/profile` | ACL read | 202 job |
| GET, POST | `/orgs/{org}/datasets/{id}/quality-rules` | ACL read / `quality.manage` | |
| PATCH, DELETE | `/orgs/{org}/quality-rules/{id}` | `quality.manage` | |
| POST | `/orgs/{org}/datasets/{id}/quality-rules/suggest` | `quality.manage` | Local suggestions from the profile (no AI) |
| GET | `/orgs/{org}/datasets/{id}/versions/{v}/quality-report` | ACL read | Includes duplicates summary |
| GET, POST | `/orgs/{org}/pipelines` | `pipelines.view` / `pipelines.manage` | |
| GET, PATCH, DELETE | `/orgs/{org}/pipelines/{id}` | as above | |
| POST | `/orgs/{org}/pipelines/{id}/versions` | `pipelines.manage` | DAG JSON validated: acyclic, typed steps, source → steps → destination |
| POST | `/orgs/{org}/pipelines/{id}/versions/{v}/publish` | `pipelines.manage` | |
| POST | `/orgs/{org}/pipelines/{id}/validate` | `pipelines.manage` | Dry run on a 1,000-row sample |
| GET, PUT, DELETE | `/orgs/{org}/pipelines/{id}/schedule` | `pipelines.manage` | Cron + timezone |
| POST | `/orgs/{org}/pipelines/{id}/runs` | `pipelines.run` | 202; Idempotency-Key |
| GET | `/orgs/{org}/pipelines/{id}/runs` · `/runs/{run_id}` | `pipelines.view` | Status, duration, rows, error, quality summary, attempts |
| GET | `/orgs/{org}/pipeline-runs/{run_id}/steps` · `/logs` | `pipelines.view` | |
| POST | `/orgs/{org}/pipeline-runs/{run_id}/retry` · `/cancel` | `pipelines.run` | |
| GET | `/orgs/{org}/lineage?node_type=&node_id=&depth=3&direction=up|down|both` | `datasets.view` | Graph `{ nodes, edges }`, filtered to visible assets |

### Phase 4: Conversational BI
| Method | Path | Perm | Notes |
|---|---|---|---|
| GET, POST | `/orgs/{org}/semantic-models` | `datasets.view` / `semantic.manage` | |
| GET, PATCH | `/orgs/{org}/semantic-models/{id}` | as above | Tables, columns, synonyms, `exposed_to_ai`, `prompt_alias` |
| POST | `/orgs/{org}/semantic-models/{id}/generate` | `semantic.manage` | Bootstraps from the profile (local); optional Gemini descriptions only if the policy allows SCHEMA for the asset |
| POST | `/orgs/{org}/semantic-models/{id}/publish` | `semantic.manage` | Builds the schema card |
| GET, POST | `/orgs/{org}/semantic-models/{id}/metrics` | `semantic.manage` | Expression validated by the guard's expression mode |
| GET, POST | `/orgs/{org}/bi/conversations` | `bi.chat` + ACL query | |
| GET, PATCH, DELETE | `/orgs/{org}/bi/conversations/{id}` | owner | |
| GET | `/orgs/{org}/bi/conversations/{id}/messages` | owner | |
| POST | `/orgs/{org}/bi/conversations/{id}/messages` | `bi.chat` + ACL query | SSE (§4.2), or JSON 202 when `Accept: application/json` |
| POST | `/orgs/{org}/bi/messages/{id}/feedback` | owner | `{ correct: bool, corrected_sql?: string }`; a verified answer is promoted into the cache |
| POST | `/orgs/{org}/bi/queries` | `bi.sql` + ACL query | Manual SQL (analysts and above). The same guard and audit apply. |
| GET | `/orgs/{org}/bi/query-history` | `bi.chat` (own) / `audit.view` (all) | From `bi_query_audit` |
| GET, POST | `/orgs/{org}/saved-queries` · PATCH/DELETE `/{id}` | `dashboards.create` | |
| GET, POST | `/orgs/{org}/dashboards` · `/{id}` · `/{id}/widgets` | `dashboards.view` / `dashboards.create` | |
| GET | `/orgs/{org}/dashboards/{id}/data` | `dashboards.view` + ACL | Executes widget queries through the guard (cached 5 min) |
| POST | `/orgs/{org}/exports` | `exports.create` + ACL export | 202. `{ source_type, source_id, format: csv|xlsx|pdf }`. CSV/XLSX formula injection is escaped. |
| GET | `/orgs/{org}/exports/{id}` · `/download` | owner | Pre-signed MinIO URL, 10-minute expiry |
| GET | `/orgs/{org}/ai/status` | auth | `{ policy, tier, org_budget_remaining, project_quota_state: ok|degraded|exhausted, resets_at }` |

### Phase 5: Governance
| Method | Path | Perm |
|---|---|---|
| GET, POST, DELETE | `/orgs/{org}/datasets/{id}/acl` | `governance.policy.edit` |
| GET, POST, PATCH, DELETE | `/orgs/{org}/datasets/{id}/column-policies` | `governance.policy.edit` |
| GET, POST, PATCH, DELETE | `/orgs/{org}/datasets/{id}/row-policies` | `governance.policy.edit` |
| POST | `/orgs/{org}/datasets/{id}/policy-preview` | `governance.policy.edit`: run a sample query **as** a chosen user (impersonated policy, no data leaves the org) |
| GET, PUT | `/orgs/{org}/users/{id}/attributes` | `governance.policy.edit` |
| GET, PATCH | `/orgs/{org}/datasets/{id}/pii-tags` | `governance.policy.edit` |
| GET, POST, PATCH, DELETE | `/orgs/{org}/retention-policies` | `governance.retention.edit` |
| GET | `/orgs/{org}/access-history` | `audit.view` |
| GET | `/orgs/{org}/usage?from=&to=&metric=` | `org.view` |
| GET | `/orgs/{org}/ai/usage?from=&to=` | `org.view` |
| GET, PATCH | `/orgs/{org}/ai/config` | `org.settings.edit` (budgets, `log_prompt_text`) |

### Phase 8: Document Intelligence
| Method | Path | Perm |
|---|---|---|
| GET, POST | `/orgs/{org}/document-collections` | `documents.view` / `documents.manage` |
| POST | `/orgs/{org}/documents` (multipart) | `documents.manage` (202: extract → OCR → chunk → embed) |
| GET, DELETE | `/orgs/{org}/documents/{id}` · `/pages/{n}` · `/entities` · `/tables` | `documents.view` |
| POST | `/orgs/{org}/documents/{id}/classification` | `datasets.classify` |
| POST | `/orgs/{org}/documents/search` | `documents.view`: hybrid (vector + full-text) search, local |
| POST | `/orgs/{org}/documents/ask` | `documents.ask`: SSE; RAG with citations; generative answers only if the policy allows `DOCUMENT_TEXT` for every retrieved asset, otherwise an extractive answer (passages only) |
| POST | `/orgs/{org}/documents/{id}/extract` | `documents.manage`: `{ schema_key }` → `extraction_result` (needs review) |
| POST | `/orgs/{org}/documents/compare` | `documents.view`: `{ a, b }` → section diff (local) + optional summary |
| POST | `/orgs/{org}/document-tables/{id}/promote` | `datasets.create` |

### Phase 9: ML Workspace
| Method | Path | Perm |
|---|---|---|
| GET, POST | `/orgs/{org}/ml/projects` · `/{id}` | `ml.view` / `ml.manage` |
| POST | `/orgs/{org}/ml/projects/{id}/eda` | `ml.manage` (202; local statistics, optional Gemini explanation per policy) |
| GET | `/orgs/{org}/ml/projects/{id}/recommendations` | `ml.view`: model recommendations with reasons |
| POST | `/orgs/{org}/ml/projects/{id}/snapshots` · `/experiments` | `ml.manage` (202) |
| GET | `/orgs/{org}/ml/experiments/{id}` · `/compare?ids=` | `ml.view` |
| POST | `/orgs/{org}/ml/experiments/{id}/register` | `ml.manage` |
| POST | `/orgs/{org}/ml/models/{id}/versions/{v}/promote` | `ml.promote` (creates an approval request) |
| POST | `/orgs/{org}/ml/models/{id}/predict` | API key scope `ml.predict` or `ml.view`: single or batch (≤ 1,000 rows), with explanation |
| POST | `/orgs/{org}/ml/models/{id}/batch-predictions` | `ml.manage` (202; writes a dataset) |
| GET | `/orgs/{org}/ml/models/{id}/monitoring` | `ml.view`: drift and performance |

### Phase 6: Customer 360
| Method | Path | Perm |
|---|---|---|
| POST | `/orgs/{org}/customers/sources` | `customers.manage`: map a dataset to customer fields |
| POST | `/orgs/{org}/customers/resolve` | `customers.manage` (202 identity resolution) |
| GET | `/orgs/{org}/customers/matches?status=pending_review` | `customers.review` |
| POST | `/orgs/{org}/customers/matches/{id}/decision` | `customers.review` `{ decision: confirm|reject }` |
| GET | `/orgs/{org}/customers` · `/{id}` · `/{id}/timeline` · `/{id}/scores` | `customers.view` |
| GET, POST | `/orgs/{org}/customers/segments` · `/{id}/members` | `customers.manage` |
| POST | `/orgs/{org}/customers/scoring-runs` | `customers.manage` (churn, CLV, NBA via the ML registry) |

### Phase 7: Product 360
| Method | Path | Perm |
|---|---|---|
| POST | `/orgs/{org}/products/sources` · `/normalize` | `products.manage` |
| GET | `/orgs/{org}/products` · `/{id}` · `/{id}/attributes` | `products.view` |
| GET | `/orgs/{org}/products/matches?status=pending_review` | `products.review` |
| POST | `/orgs/{org}/products/matches/{id}/decision` | `products.review` |
| GET | `/orgs/{org}/products/corrections?status=proposed` | `products.review` |
| POST | `/orgs/{org}/products/corrections/{id}/decision` | `products.review` (apply only on approve) |
| POST | `/orgs/{org}/products/search` | `products.view` (semantic, local embeddings) |
| POST | `/orgs/{org}/products/enrich` | `products.manage` (202; AI suggestions become proposed corrections; subject to policy) |

### Phase 10: Automation and agents
| Method | Path | Perm |
|---|---|---|
| GET, POST | `/orgs/{org}/workflows` · `/{id}` · `/{id}/versions` · `/{id}/publish` | `automation.manage` |
| GET, POST, DELETE | `/orgs/{org}/workflows/{id}/triggers` | `automation.manage` |
| POST | `/hooks/{trigger_id}` | HMAC signature with the trigger secret (no session) |
| POST | `/orgs/{org}/workflows/{id}/runs` | `automation.run` |
| GET | `/orgs/{org}/workflow-runs/{id}` · `/steps` | `automation.view` |
| GET, POST | `/orgs/{org}/agents` · `/{id}` | `automation.manage` (tools allowlist, scopes, approval rules) |
| GET | `/orgs/{org}/approvals?status=pending` | role-assigned approver |
| POST | `/orgs/{org}/approvals/{id}/decision` | assignee `{ decision, comment }` (re-authentication for `impact=high`) |

### Phase 11: Platform super-admin (`is_platform_admin`, `nexora_ops` alias)
| Method | Path | Notes |
|---|---|---|
| GET | `/platform/orgs` · `/platform/orgs/{id}` | Counts and status only; **no tenant data rows** |
| POST | `/platform/orgs/{id}/suspend` · `/reactivate` | Audited in `platform_audit_event` |
| GET | `/platform/health` | Component status (DB, Redis, MinIO, workers, beat, Gemini reachability) |
| GET | `/platform/jobs?queue=&status=` | Celery queue depths, failures |
| GET | `/platform/ai/usage` · PUT `/platform/ai/model-quotas/{model}` | Project-level Gemini usage; quota values entered from AI Studio |
| GET | `/platform/logs?level=&component=` | Structured log search (recent window) |

### Health (unauthenticated, internal network only)
`GET /healthz` (liveness) · `GET /readyz` (DB, Redis and MinIO checks) · `GET /metrics` (Prometheus, internal only)

## 6. Python service contracts (internal APIs between modules)

These are defined as Protocols and frozen dataclasses in `contracts/python/`. All agents build against them. Summary:

```python
# contracts/python/tenancy.py
TENANT_GUC = "app.org_id"
class TenantContext(Protocol):
    def __call__(self, org_id: UUID) -> ContextManager[None]: ...

# contracts/python/policy.py
class AIDataPolicy(StrEnum): SYNTHETIC_ONLY="synthetic_only"; SCHEMA_ONLY="schema_only"; AGGREGATES_ONLY="aggregates_only"; FULL="full"
class PayloadClass(StrEnum): SCHEMA="SCHEMA"; VALUES="VALUES"; DOCUMENT_TEXT="DOCUMENT_TEXT"
@dataclass(frozen=True)
class QueryPolicy:
    org_id: UUID; user_id: UUID; dataset_id: UUID
    allowed_columns: Mapping[str, frozenset[str]]       # table alias -> columns
    column_masks: Mapping[tuple[str, str], MaskSpec]
    row_predicates: Mapping[str, RowPredicate]         # table alias -> predicate AST spec + bound params
    row_cap: int; byte_cap: int; timeout_s: int
    fingerprint: bytes
class PolicyService(Protocol):
    def effective_policy(self, user_id: UUID, dataset_id: UUID) -> QueryPolicy: ...

# contracts/python/guard.py
@dataclass(frozen=True)
class GuardResult: ok: bool; reject_code: str | None; executed_sql: str | None; params: Mapping[str, Any]; tables: tuple[str, ...]
@dataclass(frozen=True)
class ResultSet: columns: tuple[ColumnMeta, ...]; rows: tuple[tuple, ...]; row_count: int; truncated: bool; duration_ms: int; plan_cost: float
class SqlGuard(Protocol):
    def validate(self, sql: str, policy: QueryPolicy, target: QueryTarget) -> GuardResult: ...
    def execute(self, result: GuardResult, policy: QueryPolicy, target: QueryTarget) -> ResultSet: ...

# contracts/python/ai.py
class GeminiService(Protocol):
    def generate_structured(self, *, org_id: UUID, feature: str, prompt_key: str, variables: Mapping, payload: PayloadManifest, schema: type[BaseModel]) -> AIResult: ...
    def stream(self, *, org_id: UUID, feature: str, prompt_key: str, variables: Mapping, payload: PayloadManifest) -> Iterator[str]: ...
class AIPolicyGate(Protocol):
    def check(self, org_id: UUID, payload: PayloadManifest) -> None: ...   # raises AIPolicyBlocked

# contracts/python/nl2sql.py
@dataclass(frozen=True)
class SqlProposal: sql: str; source: Literal["cache_exact","cache_semantic","builder","llm"]; explanation: str | None; assumptions: tuple[str, ...]; chart_hint: ChartHint | None; ai_request_ids: tuple[UUID, ...]
class NL2SQL(Protocol):
    def propose(self, question: str, ctx: QuestionContext) -> SqlProposal: ...

# contracts/python/jobs.py
class JobPublisher(Protocol):
    def publish(self, org_id: UUID, job_id: UUID, event: JobEvent) -> str: ...
```
