import { describe, expect, it } from "vitest";
import { BUILTIN_STEPS, defaultConfig, mergeCatalog, missingRequired } from "./catalog";
import {
  approvalHref,
  formatScopes,
  graphFromWire,
  graphToWire,
  mapAgent,
  mapApproval,
  mapRun,
  mapStepRun,
  mapWorkflow,
  newStepId,
  parseScopes,
  topoOrder,
  validateGraph,
  type WfGraph,
} from "./graph";

const SERVER_GRAPH = {
  steps: [
    { key: "rows", kind: "bi.saved_query", config: { saved_query_id: "q1" } },
    { key: "big", kind: "condition", depends_on: ["rows"], config: { left: "{{ steps.rows.output.row_count }}", op: "gt", right: 100 } },
    { key: "ok", kind: "approval", depends_on: ["big"], config: { title: "Ship?" }, retries: 2 },
    { key: "else_note", kind: "notify", depends_on: ["big"], when: false, config: { title: "Small day" } },
  ],
  layout: { rows: { x: 0, y: 10 }, big: { x: 260, y: 10 } },
};

describe("workflow graph wire format", () => {
  it("reads the apps.automation steps dialect into nodes and branch edges", () => {
    const g = graphFromWire(SERVER_GRAPH);
    expect(g.nodes.map((n) => n.id)).toEqual(["rows", "big", "ok", "else_note"]);
    expect(g.nodes[0].position).toEqual({ x: 0, y: 10 });
    expect(g.nodes[2].options).toEqual({ retries: 2 });
    expect(g.edges).toEqual([
      { from: "rows", to: "big" },
      { from: "big", to: "ok", branch: "true" },
      { from: "big", to: "else_note", branch: "false" },
    ]);
  });

  it("round-trips: depends_on from edges, when=false only on the no branch, layout separate", () => {
    const wire = graphToWire(graphFromWire(SERVER_GRAPH));
    expect(wire.steps).toEqual([
      { key: "rows", kind: "bi.saved_query", config: { saved_query_id: "q1" } },
      { key: "big", kind: "condition", config: SERVER_GRAPH.steps[1].config, depends_on: ["rows"] },
      { key: "ok", kind: "approval", config: { title: "Ship?" }, depends_on: ["big"], retries: 2 },
      { key: "else_note", kind: "notify", config: { title: "Small day" }, depends_on: ["big"], when: false },
    ]);
    expect(wire.layout.big).toEqual({ x: 260, y: 10 });
    expect(Object.keys(wire.steps[0])).not.toContain("position");
  });

  it("also accepts the canvas dialect {nodes, edges}", () => {
    const g = graphFromWire({ nodes: [{ id: "a", type: "wait", config: { seconds: 5 } }, { id: "b", kind: "notify" }], edges: [{ source: "a", target: "b" }, { from: "a", to: "zzz" }] });
    expect(g.nodes.map((n) => `${n.id}:${n.kind}`)).toEqual(["a:wait", "b:notify"]);
    expect(g.edges).toEqual([{ from: "a", to: "b" }]);
  });

  it("orders steps topologically and detects cycles", () => {
    const g: WfGraph = graphFromWire(SERVER_GRAPH);
    expect(topoOrder(g)?.[0]).toBe("rows");
    expect(topoOrder({ ...g, edges: [...g.edges, { from: "ok", to: "rows" }] })).toBeNull();
  });

  it("generates unique, server-valid step keys", () => {
    expect(newStepId("bi.saved_query", ["bi_saved_query_1"])).toBe("bi_saved_query_2");
    expect(newStepId("ML.Train", [])).toBe("ml_train_1");
  });
});

describe("validateGraph", () => {
  const cat = BUILTIN_STEPS;

  it("accepts a complete graph", () => {
    expect(validateGraph(graphFromWire(SERVER_GRAPH), cat)).toEqual([]);
  });

  it("flags empty graphs, missing required config, plain edges out of a condition, branch conflicts and cycles", () => {
    expect(validateGraph({ nodes: [], edges: [] }, cat)[0].message).toMatch(/at least one step/);
    const g = graphFromWire({
      steps: [
        { key: "c", kind: "condition", config: { left: "x", op: "eq" } },
        { key: "n", kind: "notify", config: {} },
        { key: "j", kind: "wait", config: { seconds: 1 } },
      ],
    });
    g.edges.push({ from: "c", to: "n" }, { from: "c", to: "j", branch: "true" }, { from: "c", to: "j", branch: "false" });
    const msgs = validateGraph(g, cat).map((i) => i.message);
    expect(msgs.some((m) => /fill in title/.test(m))).toBe(true);
    expect(msgs.some((m) => /"yes" or "no" handle/.test(m))).toBe(true);
    expect(msgs.some((m) => /both the "yes" and the "no"/.test(m))).toBe(true);
    g.edges.push({ from: "n", to: "c" });
    expect(validateGraph(g, cat).some((i) => /cycle/.test(i.message))).toBe(true);
  });

  it("rejects step ids the server would refuse", () => {
    const g = graphFromWire({ steps: [{ key: "Bad Key", kind: "wait", config: { seconds: 1 } }] });
    expect(validateGraph(g, cat)[0].message).toMatch(/lowercase/);
  });
});

describe("step catalog", () => {
  it("falls back to the CONTRACT list when the registry isn't exposed", () => {
    const steps = mergeCatalog(null);
    expect(steps.map((s) => s.kind)).toEqual(expect.arrayContaining(["condition", "approval", "pipelines.run", "customers.score", "ml.batch_predict", "documents.process"]));
    expect(steps.every((s) => !s.registered)).toBe(true);
  });

  it("overlays the server registry and keeps unknown kinds", () => {
    const steps = mergeCatalog({
      items: [
        { kind: "wait", label: "Pause", impact: "low", schema: { type: "object", properties: { seconds: { type: "integer" } } } },
        { kind: "datasets.export", impact: "high", schema: { type: "object", properties: {} } },
      ],
    });
    const wait = steps.find((s) => s.kind === "wait")!;
    expect(wait).toMatchObject({ label: "Pause", registered: true, category: "control" });
    expect(steps.find((s) => s.kind === "datasets.export")).toMatchObject({ impact: "high", category: "data", registered: true, label: "Export" });
    expect(steps.find((s) => s.kind === "notify")?.registered).toBe(false);
  });

  it("computes defaults and missing required fields", () => {
    const approval = BUILTIN_STEPS.find((s) => s.kind === "approval")!;
    expect(defaultConfig(approval.schema)).toMatchObject({ approver_permission: "automation.manage", impact: "low" });
    expect(missingRequired(approval.schema, { title: "" })).toEqual(["title"]);
  });
});

describe("payload mappers", () => {
  it("maps workflows, runs and step runs with aliases", () => {
    const wf = mapWorkflow({ id: "w", name: "Nightly", status: "active", current_version: { id: "v3", version: 3, graph: SERVER_GRAPH }, last_run: { id: "r", status: "failed", finished_at: "2026-11-20T00:00:00Z" } });
    expect(wf).toMatchObject({ current_version: 3, current_version_id: "v3", last_run: { id: "r", status: "failed" } });
    expect(wf.graph?.nodes).toHaveLength(4);
    expect(mapRun({ id: "r", workflow: { id: "w", name: "Nightly" }, status: "waiting_approval", error: { code: "x", message: "boom" } })).toMatchObject({
      workflow_id: "w",
      workflow_name: "Nightly",
      error: "boom",
    });
    expect(mapStepRun({ id: "s", step_key: "rows", kind: "bi.saved_query", status: "succeeded", attempt: 2, output: { row_count: 5 } })).toMatchObject({ attempt: 2, output: { row_count: 5 } });
  });

  it("maps approvals and links them to their subject", () => {
    const a = mapApproval({ id: "a", kind: "model_promotion", title: "Promote churn v4", impact: "high", payload: { model_id: "m1", version: 4 } });
    expect(a).toMatchObject({ impact: "high", status: "pending" });
    expect(approvalHref(a, "acme")).toBe("/o/acme/ml/models/m1");
    expect(approvalHref(mapApproval({ payload: { run_id: "r9" } }), "acme")).toBe("/o/acme/automation/runs/r9");
    expect(approvalHref(mapApproval({ payload: {} }), "acme")).toBeNull();
  });

  it("maps agents with per-tool scopes; high impact always needs approval", () => {
    const ag = mapAgent({ id: "g", name: "Ops", allowed_tools: ["pipelines.run"], tool_scopes: { "pipelines.run": { pipeline_id: ["p1", "p2"] } }, approval_required_for: ["high", "pipelines.run"] });
    expect(ag.tool_scopes).toEqual({ "pipelines.run": { pipeline_id: ["p1", "p2"] } });
    expect(ag.approval_required_for).toEqual(["high", "pipelines.run"]);
    expect(mapAgent({}).approval_required_for).toEqual(["high"]);
  });

  it("parses and formats tool scopes", () => {
    expect(parseScopes("pipeline_id = a, b; dataset_id=c")).toEqual({ scopes: { pipeline_id: ["a", "b"], dataset_id: ["c"] }, error: null });
    expect(parseScopes("nonsense").error).toMatch(/key = value/);
    expect(parseScopes("k = ").error).toMatch(/at least one/);
    expect(formatScopes({ pipeline_id: ["a", "b"] })).toBe("pipeline_id = a, b");
  });
});
