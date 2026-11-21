Owned: backend/apps/{core,accounts,orgs,governance,bi,platform_admin,automation}/**.
A. AUTOMATION (apps/automation), per API_SPECIFICATION Phase 10 + CONTRACT "W5 additions". Build it real.
   1. Models (RLS): Workflow, WorkflowVersion (validated DAG of steps), WorkflowTrigger, WorkflowRun, WorkflowStepRun,
      ApprovalRequest, AgentConfig.
   2. Step registry plus the built-in steps condition, notify, approval and wait.
   3. Runner on TenantTask (pause/resume on approval, retries, step timeouts, audit).
   4. Triggers: schedule, webhook (HMAC + timestamp; /hooks/{trigger_id}) and event. Provide events.emit.
   5. Approvals inbox and decision endpoint (re-auth when impact is high); request_approval(); the ML promotion hook per CONTRACT.
   6. Agents: tools allowlist and scopes.
   7. The bi.saved_query step. Perms automation.view, automation.manage and automation.run, seeded into roles. Isolation factories.
B. Fix: problem+json Content-Type (apps/core/exceptions.py; see CONTRACT). Make every DB test in your apps pass.
C. Tests for all of the above (DB), including webhook HMAC replay/forgery, approval re-auth, cross-org 404 and a step permission re-check.
