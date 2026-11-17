# Multi-Tenancy Design

> Status: **Design.** Last updated 2026-11-17. Owner agent: Backend (`apps/core/tenancy`), with infra SQL from the Integrator.
> **The single tenant setting name everywhere is `app.org_id`.** It is defined once as `TENANT_GUC` in `backend/apps/core/tenancy/constants.py` (mirrored in `contracts/python/tenancy.py`). A CI check fails on any other `app.*` tenant setting name in code, migrations or SQL.

## 1. Requirements
- Each organization has its own workspace, users (via memberships), database connections, datasets, AI configuration, permissions, dashboards, analytics and audit history.
- **Tenant data must never leak across organizations**, including through AI-generated SQL, caches, background jobs, files, logs or search indexes.
- Org-level usage and audit must be available. Users can belong to several orgs.
- The design must allow growth later: larger tenants on dedicated infrastructure.

## 2. Options compared

| Criterion | A. Shared DB, shared schema, `org_id` + RLS | B. Schema per tenant (e.g. django-tenants) | C. Database per tenant |
|---|---|---|---|
| Isolation strength | Logical. Good **with RLS + fail-closed app layer** | Stronger: search_path and grants | Strongest: separate DB |
| Migration effort | One migration run | N runs (slow at scale; partial failures) | N runs + N connection pools |
| Ops complexity (solo dev) | **Low** | Medium | High |
| Cross-tenant platform analytics | Easy (ops role) | Harder (union across schemas) | Hard |
| Per-tenant backup and restore | Hard (row-level export) | Possible (schema dump) | Easy |
| Noisy neighbour | Shared resources | Shared resources | Isolated |
| pgvector index efficiency | One index per table | Fragmented indexes | Fragmented |
| Blast radius of a policy bug | **All tenants** (mitigated by RLS + tests) | One schema | One DB |
| Fit for an MVP | ✅ | ⚠️ | ❌ |

## 3. Decision: hybrid

| Data | Model | Why |
|---|---|---|
| **Platform metadata** (`nexora_app`: users, orgs, datasets metadata, pipelines, audit, AI logs, vectors) | **A**: shared tables, `org_id NOT NULL`, **FORCE RLS** | Simple ops, one migration path, efficient indexes |
| **Ingested analytical data** (`nexora_wh`) | **B-style**: schema per org `org_<short_id>` + per-org reader role `wh_q_<short_id>` | Isolation for **AI-generated SQL** is enforced by **Postgres privileges**, not by application code. Even a guard bug cannot read another org's tables. |
| **Objects** (MinIO) | Prefix per org `orgs/{org_id}/…` | Key helpers require an org ID; pre-signed URLs are scoped per object |
| **Redis** | Key prefix `org:{org_id}:` | Helpers require an org ID |
| **Customer external DBs** | Per-org credentials, verified read-only roles | Customer-owned isolation |

### 3.1 Limitations (accepted and mitigated)
1. **One RLS or manager mistake could affect every tenant.** Mitigated by:
   - FORCE RLS on every tenant table;
   - the fail-closed `TenantManager`;
   - a CI check that every `TenantModel` table has the policy;
   - the per-endpoint two-org isolation suite;
   - a "no context → zero rows" test.
2. **Noisy neighbours:** a heavy org can slow others. Mitigated by per-org concurrency limits on queues (Redis semaphores), statement timeouts, and per-org AI budgets.
3. **No per-tenant restore of metadata.** An org export tool (JSON + Parquet) is planned for P13.
4. **The Gemini quota is shared at the project level.** Mitigated by per-org budgets and priority classes (`AI_ARCHITECTURE.md` §3.4).
5. **Shared Postgres cluster for the warehouse.** Mitigated by schema and role isolation. The upgrade path is in §8.

## 4. Mechanics

### 4.1 Database roles
`nexora_owner` (NOLOGIN, owns tables), `nexora_migrator` (migrations only), `nexora_app` (DML, NOBYPASSRLS), `nexora_ops` (BYPASSRLS, platform modules only), `wh_loader`, `wh_q_<short_id>`. Definitions are in `DATABASE_SCHEMA.md` §1.

### 4.2 Canonical RLS policy
Every tenant table gets this policy through the `EnableTenantRLS` migration operation:

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;      -- even the owner is subject to RLS
CREATE POLICY tenant_isolation ON <t>
  USING      (org_id = NULLIF(current_setting('app.org_id', true), '')::uuid)
  WITH CHECK (org_id = NULLIF(current_setting('app.org_id', true), '')::uuid);
```

- `current_setting('app.org_id', true)` returns NULL if the setting was never set, and `''` after a transaction-local setting expires. `NULLIF(…, '')` maps both to NULL, so `org_id = NULL` matches **zero rows**. The query does not error, and it does not leak.
- `organization` has a self-policy: `id = NULLIF(current_setting('app.org_id', true), '')::uuid`.

### 4.3 Setting the context (transaction-local only)

```python
# backend/apps/core/tenancy/constants.py
TENANT_GUC = "app.org_id"

# backend/apps/core/tenancy/context.py
current_org: ContextVar[UUID | None] = ContextVar("current_org", default=None)

@contextmanager
def tenant_context(org_id: UUID):
    token = current_org.set(org_id)
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, str(org_id)])   # true = transaction-local
            yield
    finally:
        current_org.reset(token)
```

- **Never** use `SET` (session level) or `set_config(…, false)`. Session state leaks across pooled connections (`CONN_MAX_AGE`, Django 5.1+ pooling, PgBouncer transaction mode). Transaction-local settings are safe in all of them.
- psycopg 3 option `prepare_threshold=None` (or PgBouncer ≥ 1.21 with `max_prepared_statements`) avoids prepared-statement issues under transaction pooling.

### 4.4 Request path

```mermaid
sequenceDiagram
  participant MW as TenantContextMiddleware
  participant V as TenantAPIView.initial()
  participant DB as Postgres (nexora_app)
  MW->>MW: try: (nothing set yet; DRF auth not run)
  V->>V: authenticate (session / API key)
  V->>DB: SELECT * FROM nexora_membership_check(user, org_slug)
  DB-->>V: org_id, role_id (or none → 404)
  V->>V: current_org.set(org_id); enter tenant_context(org_id)
  V->>DB: SELECT set_config('app.org_id', org_id, true)
  V->>DB: ...ORM queries (TenantManager filter + RLS)...
  V->>V: exit tenant_context (commit/rollback)
  MW->>MW: finally: clear current_org
```

- **Why the DRF view and not middleware:** DRF authentication runs at view dispatch, so middleware cannot know the user or verify membership. The middleware's only job is to **clear** the ContextVar in `finally`. It never sets the database value.
- **API keys:** `nexora_api_key_lookup(prefix)` (security definer) → verify the hash → the key's `org_id` must equal the URL org, otherwise 404.

### 4.5 ORM layer (fail-closed)

```python
class TenantManager(models.Manager):
    def get_queryset(self):
        org_id = current_org.get()
        if org_id is None:
            raise TenantContextMissing()          # fail closed (500 + alert), never "all rows"
        return super().get_queryset().filter(org_id=org_id)

class TenantModel(models.Model):
    org = models.ForeignKey("orgs.Organization", on_delete=models.RESTRICT, db_index=True)
    objects = TenantManager()
    class Meta: abstract = True
    def save(self, *a, **kw):
        if self.org_id is None: self.org_id = current_org.get()
        if self.org_id != current_org.get(): raise TenantMismatch()
        super().save(*a, **kw)
```

- An `unscoped = models.Manager()` attribute exists **only** for platform modules (`core.ops`, `platform_admin`). A ruff banned-API rule forbids `.unscoped` everywhere else.
- Every `UniqueConstraint` on a tenant model includes `org`. A Django system check (`core.checks.tenant_unique`) enforces this.
- `bulk_create`, `update()`, `raw()` and `extra()`: `raw()` and `extra()` are banned by lint. Bulk operations go through manager methods that assert the org.

### 4.6 Celery tasks

```python
class TenantTask(celery.Task):
    def __call__(self, *args, org_id: str, actor_id: str | None = None, **kwargs):
        with tenant_context(UUID(org_id)):
            # organization self-policy makes only this org's row visible; suspended/deleted orgs are skipped
            if not Organization.objects_self.filter(status="active").exists():
                raise OrgInactive(org_id)
            return self.run(*args, org_id=org_id, actor_id=actor_id, **kwargs)
```

- A `before_task_publish` signal stamps the `org_id` header from `current_org`. `task_prerun` rejects the task if the header and kwarg differ, which catches tasks enqueued for the wrong org.
- **Beat** runs only global dispatcher tasks (on the `nexora_ops` alias). They select due work (`pipeline_schedule`, retention, partition creation) and enqueue **per-org** `TenantTask`s.
- **Long tasks** use short transactions per step. One huge transaction is never held, so `tenant_context` is re-entered per unit of work.

### 4.7 SSE streams
- No database transaction is held while streaming.
- The SSE view authenticates, checks job ownership inside a short `tenant_context`, then reads the Redis Stream `org:{org_id}:job:{job_id}`.
- **Workers can write only to streams under their task's org prefix.** The helper derives the key from `current_org`.

### 4.8 Caches and vectors
- `bi_query_cache`, `document_chunk` and `product_master.embedding` are tenant tables under RLS. Vector searches always include the RLS filter.
  - pgvector HNSW with a filter can return fewer than k rows. The query uses `SET LOCAL hnsw.ef_search = 100` and over-fetches.
  - **Cache entries are never shared across orgs.**
- The semantic cache stores **pre-policy** SQL. The guard re-applies the current user's policy every time, so reusing an entry within an org cannot leak data between users with different policies.

### 4.9 Warehouse isolation
- `warehouse.provision(org)` creates `org_<short_id>` and `wh_q_<short_id>` (`DATABASE_SCHEMA.md` §5.1).
- **The executor connects as `wh_q_<short_id>` for that org only.** The DSN is chosen by `org_id` from `wh_org_binding`. The guard also rejects any schema qualifier other than the org's own.
- Two independent layers would both have to fail for a cross-org read: guard **and** Postgres privileges.

## 5. Organization lifecycle
| Event | Actions |
|---|---|
| **Create** (sign-up or super-admin) | Generate the org UUID and `short_id` → `set_config('app.org_id', new_id, true)` → insert `organization` with **`ai_data_policy='synthetic_only'`** → seed the 7 system roles + role_permission → owner membership → `ai_org_config` defaults → `warehouse.provision` (async job) → audit `org.create` |
| **Invite** | `invitation` with a hashed token, emailed. Acceptance runs through the `nexora_invitation_by_token` security-definer function → membership |
| **Suspend** (platform) | `status='suspended'` → membership checks fail → API returns 404 for the org; workers skip its tasks; warehouse role `NOLOGIN` |
| **Delete** | Soft delete → 30-day grace → `core.ops.purge_org` (`DATABASE_SCHEMA.md` §12) |
| **Usage** | `usage_daily` per metric; `ai_usage_daily` per model |

## 6. Users in multiple orgs
- A user selects the active org in the org switcher, populated by `nexora_user_orgs(user_id)`. The org is part of every URL, so there is no hidden "current org" session state.
- Opening two tabs on two orgs is safe because each request carries its own org slug.

## 7. Tenant isolation testing (CI, required)
1. **Endpoint matrix:**
   - generated from the DRF URLconf;
   - creates org A and org B with a full fixture set (one of every resource);
   - authenticates as each role in B and calls every endpoint with A's resource IDs and A's slug.
   - Expected results: **404** (or 401/403 for non-member org slugs). Response bodies are scanned for any A identifiers.
2. **Raw SQL:** connect as `nexora_app` with **no** `app.org_id` and assert `count(*) = 0` for every tenant table. With B's context, assert that none of A's rows are visible. Attempt to INSERT with A's `org_id` under B's context and expect a `WITH CHECK` violation.
3. **Warehouse:** as `wh_q_<B>`, `SELECT` from `org_<A>.*` must give a permission error. The same is checked through the guard (`table_not_allowed`).
4. **Celery:** a task enqueued with B's `org_id` cannot read A's rows. A mismatched header/kwarg is rejected.
5. **Concurrency:** 200 interleaved async requests alternating orgs A and B under uvicorn must show zero cross-reads. This catches ContextVar and pool leaks.
6. **Caches, vectors, objects:** a semantic cache lookup, a vector search and a pre-signed URL from org B never return org A's content.
7. **Static checks:**
   - every `TenantModel` has `EnableTenantRLS` in its migrations;
   - every tenant `UniqueConstraint` includes `org`;
   - no `.unscoped` outside platform modules;
   - the only tenant setting name used is `app.org_id`.

## 8. Evolution path
1. **Read replicas** for `nexora_wh` query load (route `query_wh` to the replica).
2. **Dedicated warehouse database or cluster per large tenant.** `wh_org_binding` gains a `dsn_credential_id`. The executor and loader route by org, with no code changes elsewhere.
3. **Dedicated metadata database per enterprise tenant.** A Django database router keyed on `current_org` can move a tenant later. Org IDs are already globally unique (UUIDv7), so data can be exported and imported without key conflicts.
4. **Regional data residency:** `organization.region` selects the deployment region in a multi-region future. Gemini's EEA/UK/CH paid-only rule is enforced per region.
