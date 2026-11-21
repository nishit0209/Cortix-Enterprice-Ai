Owned apps: backend/apps/{core,accounts,orgs,governance,bi,platform_admin,automation}/**.

A. LOCAL RUNTIME in apps/core. The "Local runtime" section of the CONTRACT is binding. Other agents code against these names IN PARALLEL, so keep them exact.

   1. **Task runner.** `TenantTask`, and a new non-tenant `NexoraTask` base for ops tasks, make `.delay()` and `.apply_async()` honour `settings.TASK_RUNNER`:
      - `"celery"`: normal Celery;
      - `"eager"`: run inline;
      - `"thread"`: submit to a process-wide `ThreadPoolExecutor(settings.TASK_RUNNER_THREADS)`.

      A thread run must:
      - call `close_old_connections()` before and after;
      - use `tenant_context(org_id)` and restore the actor exactly like `TenantTask.__call__`;
      - re-submit retries and backoff (`self.retry` / countdown) through a timer;
      - log exceptions and mark the job failed.

      Callers never branch on the runner.
   2. **Job events.** Add `core.JobEvent` (`org_id`, `job_id`, `seq`, `event`, `data` JSON, `created_at`; RLS; append-only).
      - `apps.core.jobs.publish()` writes rows when `JOB_EVENTS_BACKEND="db"`. Keep the Redis path.
      - The SSE view polls the rows. Keep it async-safe: `sync_to_async` short queries, no long transaction. `Last-Event-ID` = `seq`, and the keep-alive comments are unchanged.
      - Add a purge task for events older than 7 days, registered as a periodic job (see 4).
   3. **Storage.** Add a filesystem backend for `STORAGE_BACKEND="fs"` behind the SAME public API of `apps.core.storage` (put/get/open/exists/delete/presigned_get/etc.; keep every existing function signature).
      - Files live under `settings.LOCAL_STORAGE_DIR/<key>`. Writes are atomic (temp file + `os.replace`). Key validation is the same as today.
      - `presigned_get` returns a signed, expiring URL `/api/v1/files/<token>`. Sign it with `django.core.signing.TimestampSigner` and `STORAGE_URL_SIGNING_KEY`, with `max_age` = expiry.
      - Serve it from a streaming `FileResponse` view in a NEW `apps/core/urls_public.py` (with `urlpatterns`; the root URLs already include it once the module exists), with an RFC 6266 `Content-Disposition`.
      - Also fix the AI/ML W2 defect: provide a ContextVar-only tenant helper, so storage I/O in long tasks does not need an open DB transaction.
   4. **Periodic jobs.** Add the registry `apps.core.scheduler.register_periodic(name, every_seconds, fn)`.
      - With `settings.LOCAL_SCHEDULER`, a single daemon thread ticks due jobs in SERVER processes only (runserver/uvicorn/daphne). Never start it during migrate, tests, shell or other manage.py commands; env `NEXORA_SCHEDULER=0` also disables it.
      - Guard each tick with `pg_try_advisory_lock`, so only one process ticks.
      - With Celery, expose the registry to beat.
      - Register the JobEvent purge.
   5. **Health.** `/healthz` and `/readyz` report:
      - Redis `"not_configured"` (not an error) when `REDIS_URL` is empty;
      - storage `"fs"` or `"s3"`;
      - a real DB check.
   6. **Cache.** The cache is a `DatabaseCache` table `"nexora_cache"`. If the table is missing, `readyz` must report a clear error ("run manage.py createcachetable").

B. **`seed_demo` command.** In apps/platform_admin, add the management command `seed_demo [--email --password --org-name --reset]`. It must be idempotent.
   - Create a demo user (default `demo@nexora.local`). If no `--password` is given, generate a strong one and PRINT it.
   - Create an org. Its AIDataPolicy stays `synthetic_only`.
   - Ingest every CSV in `testdata/generated/`. You may read QA's files but not edit them; if they are missing, run `testdata/generate.py` via subprocess.
     - Ingest THROUGH THE PUBLIC `apps.datasets.services` (`create_upload` + `create_dataset`), with classification `synthetic` and admin attestation.
     - Force `TASK_RUNNER` to `"eager"` inside the command, so profiling, quality, the warehouse load and the semantic layer all complete.
   - Create a default dashboard with 4–6 widgets, built from deterministic-builder questions over sales through the real bi services: revenue by region, monthly revenue trend, top products, and a KPI for total revenue.
   - Create one sample conversation.
   - Print a summary: login, org slug, datasets, rows. No fake rows anywhere.

C. **Dashboards and Notifications.** Build Dashboards (apps/bi) and Notifications (apps/core) EXACTLY per the CONTRACT "W3 additions".
   - Include models + RLS + migrations, serializers, views, and audit.
   - Add the perms `dashboards.view` / `dashboards.manage`, seeded into roles like the other perms.
   - Widget data re-executes the SavedQuery through sqlguard under the VIEWER's `effective_policy`, never from stored rows. Audit it with source `"dashboard"`.
   - Provide `apps.core.notifications.notify(...)`. Produce a notification when an invitation is accepted (orgs). Other agents call `notify` for their own events.

D. **bi changes.**
   - Pass `semantics=DatasetSemantics` to nl2sql `propose` / `remember` / `store_verified` (AI/ML W2 request), so the cache is scoped to the dataset version.
   - The `ai_quota_exhausted` error event includes `resets_at`.
   - Keep the SSE and JSON shapes stable; the frontend relies on them.

E. **Isolation factories.** Add one for EVERY backend-owned tenant route with resource kwargs:
   - orgs member-detail, invitation-detail;
   - accounts api-key-detail;
   - governance policy-preview, user-attributes, dataset-acl list/detail, column-policy list/detail, row-policy list/detail;
   - bi conversations, messages, saved-queries, dashboards, widgets;
   - core notifications.

   QA's `test_every_resource_route_has_an_isolation_factory` must pass for your apps.

F. **Tests.**
   - Unit tests for:
     - the runner (thread and eager semantics, with fakes);
     - fs storage (traversal, atomicity, signing, expiry, tamper);
     - the scheduler (due-time computation; lock acquired and not acquired, with a fake).
   - DB tests for:
     - JobEvent publish and SSE replay;
     - the dashboards and notifications APIs, including cross-user and cross-org 404s and widget data under a restricted viewer policy;
     - `seed_demo`, run eager.
