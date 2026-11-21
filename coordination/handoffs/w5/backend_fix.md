# W5 fix — backend (agent/backend/w5fix)

## Done
- SECURITY: runner gates every `impact="high"` step. Before the handler runs, unless the step directly `depends_on` a
  succeeded `approval` step whose request is approved + impact high + approver `approvals.decide`, the runner creates an
  ApprovalRequest (impact high, approvals.decide, payload.gate="impact", source_id=step run) and the run goes
  `waiting_approval`. Approve (password re-auth) -> step re-armed and executed once; reject/expire -> run failed
  (`approval_rejected`); cancel -> gate approval cancelled. (apps/automation/runner.py, services.py)
- GET automation/steps returns `graph_shape` + `workflow_body_example`; POST workflows/validate returns them when invalid.
- notify accepts `message` as alias of `body`; `request_approval(requested_by=)` accepts a User or id.

## Tests (real DB)
apps/automation/tests + tests/security/test_automation_security.py: 43 passed, 1 failed (harness, below), 1 skipped.
New: 4 regression tests (+1 param) in apps/automation/tests/test_automation.py.

## Accepted workflow body (POST /workflows)
{"name": "Notify then approve", "description": "optional",
 "graph": {"steps": [
   {"key": "s1", "kind": "notify", "config": {"title": "Hello", "body": "Started"}},
   {"key": "s2", "kind": "approval", "depends_on": ["s1"], "config": {"title": "Continue?", "impact": "high"}}]}}
Edges = `depends_on` only (top-level `edges`/`nodes` are not used). Step fields: key, kind, config, depends_on, when,
name, retries, retry_backoff_s, timeout_s. Config must match the kind's schema from GET automation/steps.

## Defects for QA
- test_running_a_workflow_grants_no_permission_the_actor_lacks: NO escalation (actor is denied with
  step_permission_denied/Forbidden, no ResolutionRun). Only the positive control fails: owner's customers.resolve run
  fails with `NoCustomerSources` because the test never maps a customer source (POST /customers/sources) first.
- testing/automation.create_workflow stops on the first non-400 response; invalid graphs return 422 invalid_workflow,
  so only the first shape is ever tried (it is the correct shape).
- test_agent_plan_cannot_call_a_tool_outside_its_allowlist skips: agents are not a step kind; use POST /agents and
  POST /agents/{id}/runs {"steps":[{"tool": "<kind>", "config": {}}]} (refused 422 invalid_agent_plan / tool_not_allowed).
