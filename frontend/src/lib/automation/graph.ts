/**
 * Workflow graph (the `graph` of a workflow version: DATABASE_SCHEMA `workflow_version.graph`) and the tolerant
 * mappers for `apps.automation` payloads. Wire format sent by the builder:
 *   {nodes: [{id, kind, name, config, position}], edges: [{from, to, branch?: "true"|"false"}]}
 * Reading also accepts `steps` for nodes, `type`/`key` aliases and `source`/`target` edges.
 */

import { missingRequired, stepKind, type StepKind } from "./catalog";

export interface WfNode {
  id: string;
  kind: string;
  name: string;
  config: Record<string, unknown>;
  position: { x: number; y: number };
}

export interface WfEdge {
  from: string;
  to: string;
  branch?: "true" | "false";
}

export interface WfGraph {
  nodes: WfNode[];
  edges: WfEdge[];
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" ? v : typeof v === "number" ? String(v) : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : null);

export function graphFromWire(raw: unknown): WfGraph {
  const g = isObj(raw) ? raw : {};
  const rawNodes = Array.isArray(g.nodes) ? g.nodes : Array.isArray(g.steps) ? g.steps : [];
  const nodes: WfNode[] = [];
  rawNodes.forEach((n, i) => {
    if (!isObj(n)) return;
    const id = str(n.id) ?? str(n.key) ?? `step_${i + 1}`;
    const kind = str(n.kind) ?? str(n.type) ?? str(n.step) ?? "notify";
    const pos = isObj(n.position) ? n.position : {};
    nodes.push({
      id,
      kind,
      name: str(n.name) ?? str(n.label) ?? "",
      config: isObj(n.config) ? n.config : {},
      position: { x: num(pos.x) ?? i * 260, y: num(pos.y) ?? 80 },
    });
  });
  const ids = new Set(nodes.map((n) => n.id));
  const edges: WfEdge[] = [];
  const rawEdges = Array.isArray(g.edges) ? g.edges : [];
  for (const e of rawEdges) {
    if (!isObj(e)) continue;
    const from = str(e.from) ?? str(e.source);
    const to = str(e.to) ?? str(e.target);
    if (!from || !to || !ids.has(from) || !ids.has(to)) continue;
    const b = e.branch ?? e.when ?? e.condition;
    edges.push({ from, to, ...(b === true || b === "true" ? { branch: "true" as const } : b === false || b === "false" ? { branch: "false" as const } : {}) });
  }
  // Older/simple graphs list steps in order with `next` or no edges at all: chain them.
  if (!rawEdges.length && nodes.length > 1 && !Array.isArray(g.edges)) {
    for (let i = 1; i < nodes.length; i++) edges.push({ from: nodes[i - 1].id, to: nodes[i].id });
  }
  return { nodes, edges };
}

export function graphToWire(g: WfGraph): WfGraph {
  return {
    nodes: g.nodes.map((n) => ({ id: n.id, kind: n.kind, name: n.name, config: n.config, position: { x: Math.round(n.position.x), y: Math.round(n.position.y) } })),
    edges: g.edges.map((e) => ({ from: e.from, to: e.to, ...(e.branch ? { branch: e.branch } : {}) })),
  };
}

export function newStepId(kind: string, existing: Iterable<string>): string {
  const taken = new Set(existing);
  const base = kind.replace(/[^a-z0-9]+/gi, "_").replace(/^_|_$/g, "").toLowerCase() || "step";
  for (let i = 1; ; i++) if (!taken.has(`${base}_${i}`)) return `${base}_${i}`;
}

/** Kahn order; returns null on a cycle. */
export function topoOrder(g: WfGraph): string[] | null {
  const indeg = new Map(g.nodes.map((n) => [n.id, 0]));
  const out = new Map<string, string[]>(g.nodes.map((n) => [n.id, []]));
  for (const e of g.edges) {
    if (!indeg.has(e.from) || !indeg.has(e.to)) continue;
    indeg.set(e.to, (indeg.get(e.to) ?? 0) + 1);
    out.get(e.from)!.push(e.to);
  }
  const queue = g.nodes.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
  const order: string[] = [];
  while (queue.length) {
    const id = queue.shift()!;
    order.push(id);
    for (const t of out.get(id) ?? []) {
      indeg.set(t, (indeg.get(t) ?? 0) - 1);
      if (indeg.get(t) === 0) queue.push(t);
    }
  }
  return order.length === g.nodes.length ? order : null;
}

export interface GraphIssue {
  nodeId?: string;
  message: string;
}

export function validateGraph(g: WfGraph, catalog: StepKind[]): GraphIssue[] {
  const issues: GraphIssue[] = [];
  if (!g.nodes.length) issues.push({ message: "Add at least one step." });
  const ids = new Set<string>();
  for (const n of g.nodes) {
    if (ids.has(n.id)) issues.push({ nodeId: n.id, message: `Duplicate step id ${n.id}.` });
    ids.add(n.id);
    const k = stepKind(catalog, n.kind);
    const missing = missingRequired(k.schema, n.config);
    if (missing.length) issues.push({ nodeId: n.id, message: `${n.name || k.label}: fill in ${missing.join(", ")}.` });
    if (n.kind === "condition") {
      const outs = g.edges.filter((e) => e.from === n.id);
      if (outs.some((e) => !e.branch)) issues.push({ nodeId: n.id, message: `${n.name || "Condition"}: connect from the "yes" or "no" handle.` });
    }
  }
  if (g.nodes.length && !topoOrder(g)) issues.push({ message: "The workflow has a cycle; steps must flow one way." });
  const roots = g.nodes.filter((n) => !g.edges.some((e) => e.to === n.id));
  if (g.nodes.length > 1 && roots.length > 1) issues.push({ message: `Only one first step is allowed; ${roots.map((r) => r.name || r.id).join(", ")} have no incoming connection.` });
  return issues;
}

/* ---------------- API payload mappers ---------------- */

export interface Workflow {
  id: string;
  name: string;
  description: string;
  status: string;
  current_version: number | null;
  current_version_id: string | null;
  owner: string | null;
  updated_at: string | null;
  created_at: string | null;
  last_run: { id: string; status: string; at: string | null } | null;
  graph: WfGraph | null;
}

export interface WorkflowVersion {
  id: string;
  version: number;
  published: boolean;
  published_at: string | null;
  created_at: string | null;
  graph: WfGraph;
}

export interface Trigger {
  id: string;
  type: string;
  config: Obj;
  is_active: boolean;
  created_at: string | null;
  last_fired_at: string | null;
  url: string | null;
}

export interface WorkflowRun {
  id: string;
  workflow_id: string | null;
  workflow_name: string | null;
  version: number | null;
  status: string;
  trigger_type: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  approval_id: string | null;
}

export interface StepRun {
  id: string;
  step_key: string;
  kind: string;
  status: string;
  attempt: number;
  output: unknown;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  approval_id: string | null;
}

export interface Approval {
  id: string;
  kind: string;
  title: string;
  status: string;
  impact: string;
  approver_permission: string | null;
  payload: Obj;
  requested_by: string | null;
  decided_by: string | null;
  decided_at: string | null;
  comment: string | null;
  created_at: string | null;
  expires_at: string | null;
  link: string | null;
}

export interface AgentConfig {
  id: string;
  name: string;
  description: string;
  model_tier: string;
  allowed_tools: string[];
  scopes: string[];
  max_steps: number;
  approval_required_for: string[];
  is_active: boolean;
  updated_at: string | null;
}

const errText = (v: unknown): string | null => (typeof v === "string" ? v : isObj(v) ? (str(v.message) ?? str(v.detail) ?? str(v.code) ?? JSON.stringify(v)) : null);
const person = (v: unknown): string | null => (isObj(v) ? (str(v.email) ?? str(v.full_name) ?? str(v.name) ?? str(v.id)) : str(v));
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.map(str).filter((x): x is string => !!x) : []);

export function mapWorkflow(raw: unknown): Workflow {
  const r = isObj(raw) ? raw : {};
  const cv = r.current_version;
  const last = isObj(r.last_run) ? r.last_run : null;
  const graphRaw = r.graph ?? (isObj(cv) ? cv.graph : undefined);
  return {
    id: str(r.id) ?? "",
    name: str(r.name) ?? "Untitled workflow",
    description: str(r.description) ?? "",
    status: str(r.status) ?? "draft",
    current_version: num(isObj(cv) ? cv.version : cv) ?? num(r.version),
    current_version_id: str(r.current_version_id) ?? (isObj(cv) ? str(cv.id) : null),
    owner: person(r.owner ?? r.owner_id),
    updated_at: str(r.updated_at),
    created_at: str(r.created_at),
    last_run: last ? { id: str(last.id) ?? "", status: str(last.status) ?? "", at: str(last.finished_at) ?? str(last.started_at) ?? str(last.created_at) } : null,
    graph: graphRaw !== undefined && graphRaw !== null ? graphFromWire(graphRaw) : null,
  };
}

export function mapVersion(raw: unknown): WorkflowVersion {
  const r = isObj(raw) ? raw : {};
  return {
    id: str(r.id) ?? "",
    version: num(r.version) ?? 0,
    published: !!r.published_at || r.is_published === true || r.status === "published",
    published_at: str(r.published_at),
    created_at: str(r.created_at),
    graph: graphFromWire(r.graph ?? r.definition ?? r),
  };
}

export function mapTrigger(raw: unknown): Trigger {
  const r = isObj(raw) ? raw : {};
  return {
    id: str(r.id) ?? "",
    type: str(r.type) ?? str(r.kind) ?? "manual",
    config: isObj(r.config) ? r.config : {},
    is_active: r.is_active !== false && r.active !== false,
    created_at: str(r.created_at),
    last_fired_at: str(r.last_fired_at) ?? str(r.last_triggered_at),
    url: str(r.url) ?? str(r.hook_url) ?? str(r.webhook_url),
  };
}

export function mapRun(raw: unknown): WorkflowRun {
  const r = isObj(raw) ? raw : {};
  const wf = isObj(r.workflow) ? r.workflow : null;
  const trig = isObj(r.trigger) ? r.trigger : null;
  return {
    id: str(r.id) ?? str(r.run_id) ?? "",
    workflow_id: str(r.workflow_id) ?? (wf ? str(wf.id) : null),
    workflow_name: str(r.workflow_name) ?? (wf ? str(wf.name) : null),
    version: num(r.version) ?? num(r.workflow_version),
    status: str(r.status) ?? "queued",
    trigger_type: str(r.trigger_type) ?? (trig ? str(trig.type) : str(r.trigger)),
    error: errText(r.error),
    started_at: str(r.started_at),
    finished_at: str(r.finished_at),
    created_at: str(r.created_at) ?? str(r.started_at),
    approval_id: str(r.approval_id) ?? str(r.pending_approval_id),
  };
}

export function mapStepRun(raw: unknown): StepRun {
  const r = isObj(raw) ? raw : {};
  return {
    id: str(r.id) ?? str(r.step_key) ?? "",
    step_key: str(r.step_key) ?? str(r.key) ?? str(r.node_id) ?? "",
    kind: str(r.kind) ?? str(r.type) ?? "",
    status: str(r.status) ?? "",
    attempt: num(r.attempt) ?? 1,
    output: r.output ?? r.output_ref ?? null,
    error: errText(r.error),
    started_at: str(r.started_at),
    finished_at: str(r.finished_at),
    approval_id: str(r.approval_id),
  };
}

export function mapApproval(raw: unknown): Approval {
  const r = isObj(raw) ? raw : {};
  const payload = isObj(r.payload) ? r.payload : {};
  return {
    id: str(r.id) ?? "",
    kind: str(r.kind) ?? str(r.source_type) ?? "approval",
    title: str(r.title) ?? str(payload.title) ?? "Approval request",
    status: str(r.status) ?? "pending",
    impact: str(r.impact) ?? "low",
    approver_permission: str(r.approver_permission),
    payload,
    requested_by: person(r.requested_by),
    decided_by: person(r.decided_by),
    decided_at: str(r.decided_at),
    comment: str(r.comment),
    created_at: str(r.created_at),
    expires_at: str(r.expires_at),
    link: str(r.link) ?? str(r.url),
  };
}

export function mapAgent(raw: unknown): AgentConfig {
  const r = isObj(raw) ? raw : {};
  const scopes = Array.isArray(r.scopes) ? strings(r.scopes) : Array.isArray(r.tool_scopes) ? strings(r.tool_scopes) : isObj(r.tool_scopes) ? Object.keys(r.tool_scopes) : [];
  const rules = r.approval_rules ?? r.approval_required_for;
  return {
    id: str(r.id) ?? "",
    name: str(r.name) ?? "Agent",
    description: str(r.description) ?? "",
    model_tier: str(r.model_tier) ?? "fast",
    allowed_tools: strings(r.allowed_tools ?? r.tools),
    scopes,
    max_steps: num(r.max_steps) ?? 8,
    approval_required_for: Array.isArray(rules) ? strings(rules) : isObj(rules) && Array.isArray(rules.impact) ? strings(rules.impact) : ["high"],
    is_active: r.is_active !== false,
    updated_at: str(r.updated_at) ?? str(r.created_at),
  };
}

/** Where an approval's subject lives in the UI (ML promotion, workflow run), from its payload. */
export function approvalHref(a: Approval, slug: string): string | null {
  const p = a.payload;
  const id = (k: string) => (typeof p[k] === "string" ? (p[k] as string) : null);
  if (a.link && a.link.startsWith("/")) return a.link.startsWith("/o/") ? a.link : `/o/${slug}${a.link}`;
  if (id("model_id")) return `/o/${slug}/ml/models/${id("model_id")}`;
  if (id("run_id")) return `/o/${slug}/automation/runs/${id("run_id")}`;
  if (id("workflow_id")) return `/o/${slug}/automation/workflows/${id("workflow_id")}`;
  return null;
}
