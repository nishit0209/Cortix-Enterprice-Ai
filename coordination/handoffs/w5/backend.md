# W5 handoff: backend

## Done
- **apps.automation, built for real:**
  - **Models (all with RLS):** Workflow, WorkflowVersion, WorkflowTrigger, WebhookDelivery, AgentConfig, WorkflowRun, WorkflowStepRun and ApprovalRequest.
  - **Two SECURITY DEFINER lookups:** `nexora_webhook_trigger_org` and `nexora_automation_due_orgs`. They return ids only.
- **Step registry** (`apps.automation.registry`):
  - `register_step(kind, handler, *, schema, impact, permission=None, label, description)`. The config is validated with JSON Schema.
  - Handlers can use `StepError` and `PauseRun`.
  - Built-in steps: `condition`, `notify`, `approval` and `wait`.
  - `bi.saved_query` is registered by `apps.bi` (`apps/bi/steps.py`). It runs through the guard under the actor's CURRENT policy, and it is audited.
- **Graphs:**
  - A graph is a validated DAG: unique keys, registered kinds, config schemas, `depends_on`, no cycles, plus `retries`, `retry_backoff_s` and `timeout_s` limits.
  - A step can branch on a `condition` step with `when: true|false`.
  - Step configs can use `{{ input.* | trigger.* | steps.<key>.output.* }}` templates.
- **Runner** (long-running `TenantTask`):
  - Each step runs in one transaction that holds the run row lock (`FOR UPDATE`).
  - Before every step, the actor's membership and permissions are re-read. The actor needs `automation.run` plus the step's own `permission`; otherwise the step fails with `step_permission_denied` or `actor_not_member`.
  - Retries happen immediately, or with a backoff through the dispatcher.
  - The step timeout is enforced twice: as a transaction-local `statement_timeout`, and at the step boundary (`step_timeout`). A late result is rolled back.
  - Pauses: `waiting_approval` (approval) and `waiting` (wait step or retry backoff).
  - Every run has a core Job, so SSE works through `/jobs/<id>/events`.
  - Everything is audited: `automation.run.*` and `automation.step.*`. The actor is notified when a run fails.
- **Triggers:**
  - `schedule`: a 5-field UTC `cron` or `every_minutes`. The periodic job `automation.dispatch` runs every 15 s.
  - `webhook`: `POST /api/v1/hooks/{trigger_id}`, served by `apps/core/hooks.py` through a core registry, so core never imports automation.
    - Headers: `X-Nexora-Timestamp` (within ±300 s) and `X-Nexora-Signature: sha256=HMAC(secret, "<ts>." + raw body)`.
    - A replayed request returns 409 `webhook_replayed` (unique digest).
    - The secret is AES-GCM encrypted at rest and shown exactly once. It can be rotated.
  - `event`: `apps.automation.events.emit(kind, payload, *, org_id=None)`. It never raises, it is a no-op when nothing matches, and it applies an exact-value `filter`.
  - `manual`.
- **Approvals:**
  - `services.request_approval(*, kind, title, payload, approver_permission, impact, requested_by, source_id=None, expires_in=7d)`. Approvers are notified.
  - Inbox: `GET /approvals?status=&mine=`. An approval is visible to holders of its approver permission and to the requester; anyone else gets 404.
  - `POST /approvals/{id}/decision {decision, comment, password}`:
    - `impact=high` needs the password (400 `reauth_failed`).
    - API keys are refused.
    - 409 `approval_not_pending` / `approval_expired`.
  - Expiry is handled by the dispatcher.
  - Handlers: `register_approval_handler(kind, fn)`. Built in: `workflow_step` and `agent_action` resume the run; `model_promotion` calls `apps.ml.services.apply_promotion(approval)` when approved. If the hook fails, the response is 422 `approval_apply_failed` and the approval stays pending.
- **Agents:**
  - CRUD covers the tools allowlist (registered kinds only, never `approval`), `tool_scopes` ({tool: {config key: [allowed values]}}), `max_steps` (at most 20), `approval_required_for` (`high` is always included) and `model_tier`.
  - `POST /agents/{id}/runs {goal?, steps?}`:
    - Without `steps`, Gemini plans the run through `GeminiService` with `PayloadManifest({SCHEMA: ()})`, sending only the goal and tool metadata.
    - The plan is checked against the allowlist, scopes, schemas, `max_steps` and the caller's permissions (422 `invalid_agent_plan`, listing every error).
    - The plan runs as an audited workflow run, with an `agent_action` approval step inserted before high-impact or listed tools.
- **Routes** (under `/orgs/{org}/`):
  - `workflows` (GET, POST), `workflows/validate`, `workflows/{id}` (GET, PATCH, DELETE), `/versions` (GET, POST), `/publish`, `/triggers` (GET, POST), `/triggers/{tid}` (GET, PATCH `is_active`, DELETE), `/triggers/{tid}/rotate-secret`, `/runs` (GET, POST returns 202).
  - `workflow-runs` (`?status&workflow_id&agent_id`), `workflow-runs/{id}`, `/steps`, `/cancel`.
  - `automation/steps` (the step catalog with schemas, plus event kinds).
  - `agents`, `agents/{id}`, `agents/{id}/runs`.
  - `approvals`, `approvals/{id}`, `approvals/{id}/decision`.
- **Permissions:** orgs migration 0005 and the catalog grant the automation permissions to every existing org. Owner and Admin get all of them, Data Engineer gets view, manage and run, Data Scientist gets view and run, and Analyst and Auditor get view. `orgs.services.member_permissions(user_id)` is new.
- **Isolation:** factories for all 14 automation resource routes are in `apps/automation/tests/isolation.py`.
- **problem+json:** `apps/core/exceptions.py` already sets `response.content_type`. Tests assert the exact header.

## Tests (real Postgres, PYTEST_DB_SUFFIX=backend)
- **Owned suites** (core, accounts, orgs, governance, bi, platform_admin, automation): **584 passed, 0 failed, 1 skipped** (a symlink privilege on Windows).
  - Automation alone: 29 DB tests. They cover HMAC replay and forgery, stale and future timestamps, secret rotation, approval re-auth, cross-org 404, step permission re-check, actor removal, timeout, retries and backoff, wait, schedule, event, agents with a fake Gemini, the ML hook, and `bi.saved_query`.
- **tests/isolation, including the new automation routes:** 1,772 passed.
- **tests/security, filtered to role/rls/append-only:** 2 failures. Both are the known QA test-side issues, unrelated to this change: `test_retention_usage.py` reads `platform_admin_*` without the ops alias.
- **Static checks:** conventions OK, ownership OK, ruff OK, `manage.py check` OK, `makemigrations --check` reports no changes, and import-linter keeps 2 of 2 contracts.

## Requests
- **Integrator (CONTRACT, as-built):**
  - The webhook path is `/api/v1/hooks/{id}`, served by `core.urls_public` (name `automation-webhook`).
  - The GET routes for workflows, agents and runs use `automation.view`. Writes use `automation.manage`. Phase 10 says manage for GET; view is used so that read-only roles can see the screens.
  - Run status `waiting` was added.
  - New notification kinds: `automation.run_failed`, `automation.notify`, `approval.requested|approved|rejected`. Their links are `/automation/runs/<id>` and `/automation/approvals/<id>`.
  - Additional routes: `validate`, `rotate-secret`, `cancel`, `workflow-runs` list, and `automation/steps`.
- **ai_ml:**
  - Implement `apps.ml.services.apply_promotion(approval)`. The approval payload should hold `{model_id, version}`, with `source_id` set to the version id.
  - Make promotion to production call `request_approval(kind="model_promotion", approver_permission="ml.promote", impact="high", ...)` and return 202 `{approval_id}`.
  - Register the steps `ml.batch_predict`, `ml.train` and `documents.process` in `ready()`. Pass `permission=` (for example `ml.manage`) so the runner re-checks it.
  - Emit `document.processed`.
- **data_engineering:**
  - Register the steps `pipelines.run`, `customers.resolve`, `customers.score` and `products.normalize`, each with its `permission=`.
  - Emit `dataset.ingest_finished`, `pipeline.run_failed` and `pipeline.run_succeeded` via `apps.automation.events.emit` (lazy import).
- **frontend:**
  - Approvals inbox: send `password` when `requires_reauth`.
  - Show the webhook `secret` once, from the POST or rotate response.
  - Run detail: steps poll or SSE via `job_id`. Show `output_redacted` for runs of other users.

## Open issues / declined
- **QA's TRUNCATE trigger request is not done.** pytest-django flushes transactional tests with TRUNCATE, so a BEFORE TRUNCATE trigger on append-only tables would break QA's own suites. Production `nexora_app` has no TRUNCATE grant anyway.

## Defects for others
- **qa_security:** `tests/security/test_retention_usage.py` (2 tests) still reads `platform_admin_platformauditevent` as `nexora_app`. It should use the `ops` alias or skip `platform_admin_*`. This was carried over from W4.
