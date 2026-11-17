# Deployment Architecture

> Status: **Design.** Last updated 2026-11-17. Owner: Integrator (`infra/**`, `docker-compose*.yml`, `.github/**`).
> **Goal:** the same container image and Compose topology in development and in single-VM production. Scale later without re-architecting.

## 1. Environments
| Environment | Purpose | Topology | Data |
|---|---|---|---|
| `dev` (Windows + Docker Desktop) | Daily development, agent waves | `docker-compose.yml` + `docker-compose.dev.yml` (bind mounts, hot reload, Mailpit) | Synthetic only |
| `ci` | Automated tests | GitHub Actions service containers | Synthetic fixtures |
| `staging` (optional, P13) | Release candidate validation | Single small VM, `docker-compose.prod.yml` | Synthetic only |
| `prod` (P13) | Pilot customers | Single VM (4 vCPU / 16 GB / 200 GB SSD) + managed backups, `docker-compose.prod.yml` | Real data **only** with a paid Gemini tier or Vertex for AI (the free tier keeps `synthetic_only`/`schema_only`) |

## 2. Container images
| Image | Base | Contents |
|---|---|---|
| `nexora-backend` | `python:3.12-slim` (multi-stage, uv) | Django app, Celery; Tesseract + `eng`/`hin`/`guj` traineddata; non-root user; no compilers in the final stage |
| `nexora-backend-ml` | `nexora-backend` + sentence-transformers model weights baked in (`multilingual-e5-base`) | `ml` worker (avoids runtime model download) |
| `nexora-web` | `node:24-alpine` (multi-stage, `next build` standalone) | Next.js server |
| Third-party | `pgvector/pgvector:pg17`, `redis:7-alpine`, `minio/minio`, `caddy:2-alpine`, `axllent/mailpit` (dev) | Pinned digests |

All images are pinned by digest and scanned with Trivy in CI. Builds are reproducible from lockfiles (`uv.lock`, `package-lock.json`).

## 3. Compose topology (prod)

```yaml
# docker-compose.prod.yml (abridged)
services:
  caddy:     { image: caddy:2-alpine, ports: ["80:80","443:443"], networks: [edge] }
  web:       { image: nexora-web, networks: [edge] }
  api:       { image: nexora-backend, command: uvicorn nexora.asgi:application --host 0.0.0.0 --port 8000 --workers 2, networks: [edge, core] }
  worker-ingest:  { image: nexora-backend, command: celery -A nexora worker -Q ingest --concurrency 2, networks: [core] }
  worker-query:   { image: nexora-backend, command: celery -A nexora worker -Q query_wh --concurrency 4, networks: [core] }
  worker-ext:     { image: nexora-backend, command: celery -A nexora worker -Q query_ext --concurrency 2, networks: [core, egress] }
  worker-llm:     { image: nexora-backend, command: celery -A nexora worker -Q llm --concurrency 2, networks: [core, egress] }
  worker-ml:      { image: nexora-backend-ml, command: celery -A nexora worker -Q ml --concurrency 1, networks: [core] }
  worker-default: { image: nexora-backend, command: celery -A nexora worker -Q default --concurrency 2, networks: [core, egress] }
  beat:      { image: nexora-backend, command: celery -A nexora beat -S django_celery_beat.schedulers:DatabaseScheduler, networks: [core] }
  postgres:  { image: pgvector/pgvector:pg17, volumes: [pgdata:/var/lib/postgresql/data, ./infra/postgres/init:/docker-entrypoint-initdb.d:ro], networks: [core] }
  redis:     { image: redis:7-alpine, command: redis-server --appendonly yes, networks: [core] }
  minio:     { image: minio/minio, command: server /data --console-address :9001, networks: [core] }
networks:
  edge: {}
  core: { internal: true }        # no internet
  egress: {}                      # only workers that must reach Gemini / customer DBs / email
```

- **Exposure:** only Caddy publishes ports. Postgres, Redis and MinIO are reachable only on `core`.
- **Egress:** `worker-ext` (customer DBs), `worker-llm` (Gemini) and `worker-default` (email) are the only containers with internet egress. The API container does not call Gemini directly; all AI work goes through the `llm` queue, except SSE token streaming, which is relayed through Redis Streams.
- **Resource limits** (`deploy.resources.limits`) are set per service. `worker-ml` gets the most RAM (6 GB).
- **Caddy for SSE:** `flush_interval -1` on `/api/*/events` and the chat stream routes. Security headers from `SECURITY_ARCHITECTURE.md` §8.

## 4. Configuration and secrets
- All configuration is in env vars, documented in `.env.example` (Integrator-owned):
  - **Core:** `DJANGO_SETTINGS_MODULE`, `SECRET_KEY`, `ALLOWED_HOSTS`, `DATABASE_URL_APP`, `DATABASE_URL_MIGRATOR`, `DATABASE_URL_OPS`, `WH_DATABASE_URL_LOADER`, `REDIS_URL`, `S3_*`.
  - **Secrets and AI:** `NEXORA_KEK`, `API_KEY_PEPPER`, `GEMINI_*`, `EMBEDDING_*`, `AI_CONSENT_TEXT_VERSION`.
  - **Other:** `EMAIL_*`, `SENTRY_DSN`, `ALLOW_PRIVATE_DB_HOSTS` (dev only), `DEV_AGENT_RPD`.
- **Production secrets:** Docker secrets or an `.env` file readable only by root on the VM (MVP). A cloud secret manager/KMS is P13+. **Never commit secrets.** gitleaks runs in pre-commit and CI.
- **Settings checks:** `DEBUG=False`, secure cookies, HSTS, and `GEMINI_TIER` consistent with each org's policy at boot.

## 5. CI/CD (GitHub Actions)
1. **On every push:** the pipeline from `TESTING_STRATEGY.md` §9 runs (lint, types, ownership, contract diff, unit/API/DB/isolation/security, frontend, scans).
2. **On merge to `main`:** build and tag images `sha-<commit>`, push them to the registry (GHCR), deploy to staging if configured, and run smoke tests.
3. **Release (tag `vX.Y.Z`):**
   - promote the images;
   - on the prod VM, `docker compose pull && docker compose run --rm migrate && docker compose up -d` (the migrate job uses `DATABASE_URL_MIGRATOR`);
   - run health checks, then smoke tests;
   - **auto-rollback** to the previous tag if `/readyz` fails for 2 minutes.
4. **Migrations** must be backward-compatible for one release (expand/contract), so the previous image keeps working during rollout.

## 6. Data protection and backups
| Item | Method | RPO / RTO target |
|---|---|---|
| Postgres (`nexora_app`, `nexora_wh`) | Nightly `pg_dump -Fc` per DB to off-VM S3-compatible storage (encrypted) + WAL archiving (pgBackRest) in prod | RPO ≤ 15 min (WAL), RTO ≤ 4 h |
| MinIO | Bucket replication / nightly sync to off-VM storage | RPO 24 h |
| Redis | AOF (broker state is recoverable; it is not a system of record) | n/a |
| Secrets (KEK) | Stored separately from backups. **Losing the KEK means losing the credentials.** Documented recovery procedure. | — |

**A restore drill runs before go-live and every quarter.** It is documented in `infra/runbooks/restore.md`.

## 7. Observability
- **Logs:** structlog JSON to stdout → Docker log driver → Loki (optional) or the VM journal. Fields: `request_id`, `org_id`, `user_id`, `job_id`, `queue`.
- **Metrics:** `django-prometheus` + Celery exporter + Postgres exporter + Redis exporter → Prometheus → Grafana dashboards:
  - API latency and error rate;
  - queue depth and age;
  - pipeline success rate;
  - guard rejects by code;
  - Gemini requests, 429s, breaker state, quota usage vs `ai_model_quota`;
  - DB connections;
  - worker memory.
- **Errors:** Sentry (self-hosted or free tier) with PII scrubbing; `send_default_pii=False`.
- **Alerts:**
  - API 5xx > 2% for 5 min;
  - queue age > 10 min;
  - pipeline failure spikes;
  - `tenant_context_missing` > 0;
  - security reject spikes;
  - Gemini breaker `open_day`;
  - disk > 80%;
  - backup failures;
  - an audit chain break.
- **Super-admin dashboard (P11)** reads these metrics and the `platform_*` tables.

## 8. Operations runbooks (`infra/runbooks/`, P13)
`deploy.md`, `rollback.md`, `restore.md`, `rotate-secrets.md` (KEK re-wrap, `SECRET_KEY`, Gemini key, warehouse role passwords), `suspend-org.md`, `incident-response.md`, `gemini-quota.md` (what to do when quotas change: update `ai_model_quota`, adjust budgets, communicate).

## 9. Paid services: identified, not assumed
| Need | Free / MVP option | Paid option (when required) | Required when |
|---|---|---|---|
| **Gemini API** | Free tier (`synthetic_only`/`schema_only`, tiny quotas) | **Gemini paid tier or Gemini on Vertex AI (still Gemini only)** | Any real or confidential data sent to AI; EEA/UK/CH users; production-level usage |
| Hosting | Local Docker | Cloud VM (~4 vCPU / 16 GB) | Any shared or pilot deployment |
| Object storage | MinIO on the VM | Managed S3-compatible storage | Durability beyond a single VM |
| Backups (off-site) | — | S3-compatible bucket | Production |
| Transactional email | Mailpit (dev) | SMTP or email API provider | Invitations and alerts in production |
| Secret manager / KMS | Env/Docker secrets | Cloud KMS | Stronger key custody (recommended for enterprise pilots) |
| CI minutes | GitHub Actions free allowance (generous for public repos, limited for private) | Paid minutes | Heavy nightly suites on a private repo |
| Error tracking | Sentry free tier / self-host | Sentry paid | Higher volume |
| Domain + TLS | Let's Encrypt (free) via Caddy | Domain registration (low cost) | Any public deployment |

**Nothing in the MVP requires a paid AI provider.** ML, OCR, embeddings, profiling and dedup run locally on open-source software.

## 10. Scaling path (no re-architecture)
1. **Vertical:** a bigger VM, and separate VMs for `worker-ml` and `postgres`.
2. **Managed Postgres** (with the pgvector extension available) + a read replica for `query_wh`.
3. **Horizontal:** multiple `api` and worker replicas behind Caddy or a cloud load balancer. Redis is already the shared state.
4. **Container orchestration** (ECS, Cloud Run jobs or Kubernetes) only when there are more than ~5 hosts.
5. **Per-tenant dedicated warehouse** for large tenants (`MULTI_TENANCY_DESIGN.md` §8).
