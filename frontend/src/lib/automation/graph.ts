/**
 * Workflow graph (`workflow_version.graph`), in the `apps.automation.graph` dialect:
 *   {steps: [{key, kind, name?, config, depends_on?: [key], when?: bool, retries?, retry_backoff_s?, timeout_s?}],
 *    layout: {<key>: {x, y}}}
 * A step that depends on a `condition` runs only when the condition's result equals its `when` (default true); the
 * builder draws that as the condition's "yes"/"no" handle. `layout` only stores canvas positions.
 * Reading also accepts the canvas dialect ({nodes, edges}) and `id`/`type` aliases.
 */

import { missingRequired, stepKind, type StepKind } from "./catalog";

export interface StepOptions {
  retries?: number;
  retry_backoff_s?: number;
  timeout_s?: number;
}

export interface WfNode {
  id: string;
  kind: string;
  name: string;
  config: Record<string, unknown>;
  position: { x: number; y: number };
  options?: StepOptions;
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

export interface WireStep {
  key: string;
  kind: string;
  name?: string;
  config: Record<string, unknown>;
  depends_on?: string[];
  when?: boolean;
  retries?: number;
  retry_backoff_s?: number;
  timeout_s?: number;
}

export interface WireGraph {
  steps: WireStep[];
  layout: Record<string, { x: number; y: number }>;
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" ? v : typeof v === "number" ? String(v) : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : null);
const OPTION_KEYS = ["retries", "retry_backoff_s", "timeout_s"] as const;

export function graphFromWire(raw: unknown): WfGraph {
  const g = isObj(raw) ? raw : {};
  const rawNodes = Array.isArray(g.steps) ? g.steps : Array.isArray(g.nodes) ? g.nodes : [];
  const layout = isObj(g.layout) ? g.layout : {};
  const nodes: WfNode[] = [];
  const deps: Array<{ to: string; from: string[]; when: unknown }> = [];
  rawNodes.forEach((n, i) => {
    if (!isObj(n)) return;
    const id = str(n.key) ?? str(n.id) ?? `step_${i + 1}`;
    const pos = isObj(layout[id]) ? (layout[id] as Obj) : isObj(n.position) ? n.position : {};
    const options: StepOptions = {};
    for (const k of OPTION_KEYS) {
      const v = num(n[k]);
      if (v !== null) options[k] = v;
    }
    nodes.push({
      id,
      kind: str(n.kind) ?? str(n.type) ?? "notify",
      name: str(n.name) ?? "",
      config: isObj(n.config) ? n.config : {},
      position: { x: num(pos.x) ?? i * 260, y: num(pos.y) ?? 80 },
      ...(Object.keys(options).length ? { options } : {}),
    });
    if (Array.isArray(n.depends_on)) deps.push({ to: id, from: n.depends_on.map(str).filter((x): x is string => !!x), when: n.when });
  });
  const kindOf = new Map(nodes.map((n) => [n.id, n.kind]));
  const edges: WfEdge[] = [];
  for (const d of deps) {
    for (const from of d.from) {
      if (!kindOf.has(from)) continue;
      const branch = kindOf.get(from) === "condition" ? (d.when === false ? "false" : "true") : undefined;
      edges.push({ from, to: d.to, ...(branch ? { branch } : {}) });
    }
  }
  if (Array.isArray(g.edges)) {
    for (const e of g.edges) {
      if (!isObj(e)) continue;
      const from = str(e.from) ?? str(e.source);
      const to = str(e.to) ?? str(e.target);
      if (!from || !to || !kindOf.has(from) || !kindOf.has(to)) continue;
      const b = e.branch ?? e.when;
      edges.push({ from, to, ...(b === true || b === "true" ? { branch: "true" as const } : b === false || b === "false" ? { branch: "false" as const } : {}) });
    }
  }
  return { nodes, edges };
}

/** The canvas as the server's `steps` graph (dependencies from incoming edges, `when` from a condition branch). */
export function graphToWire(g: WfGraph): WireGraph {
  const order = topoOrder(g) ?? g.nodes.map((n) => n.id);
  const byId = new Map(g.nodes.map((n) => [n.id, n]));
  const steps: WireStep[] = [];
  const layout: WireGraph["layout"] = {};
  for (const id of order) {
    const n = byId.get(id);
    if (!n) continue;
    const incoming = g.edges.filter((e) => e.to === id);
    const branch = incoming.find((e) => e.branch)?.branch;
    const step: WireStep = { key: n.id, kind: n.kind, config: n.config };
    if (n.name.trim()) step.name = n.name.trim();
    if (incoming.length) step.depends_on = [...new Set(incoming.map((e) => e.from))];
    if (branch === "false") step.when = false;
    for (const k of OPTION_KEYS) if (n.options?.[k] !== undefined) step[k] = n.options[k];
    steps.push(step);
    layout[n.id] = { x: Math.round(n.position.x), y: Math.round(n.position.y) };
  }
  return { steps, layout };
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
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(n.id)) issues.push({ nodeId: n.id, message: `Step id ${n.id}: lowercase letters, digits and _ only.` });
    ids.add(n.id);
    const branches = new Set(g.edges.filter((e) => e.to === n.id && e.branch).map((e) => e.branch));
    if (branches.size > 1) issues.push({ nodeId: n.id, message: `${n.name || n.id}: it can't follow both the "yes" and the "no" branch.` });
    const k = stepKind(catalog, n.kind);
    const missing = missingRequired(k.schema, n.config);
    if (missing.length) issues.push({ nodeId: n.id, message: `${n.name || k.label}: fill in ${missing.join(", ")}.` });
    if (n.kind === "condition") {
      const outs = g.edges.filter((e) => e.from === n.id);
      if (outs.some((e) => !e.branch)) issues.push({ nodeId: n.id, message: `${n.name || "Condition"}: connect from the "yes" or "no" handle.` });
    }
  }
  if (g.nodes.length && !topoOrder(g)) issues.push({ message: "The workflow has a cycle; steps must flow one way." });

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
  next_run_at: string | null;
  url: string | null;
  secret_hint: string | null;
  signature: { timestamp_header: string | null; signature_header: string | null; algorithm: string | null; format: string | null } | null;
}

export interface WorkflowRun {
  id: string;
  workflow_id: string | null;
  workflow_name: string | null;
  version: number | null;
  status: string;
  trigger_type: string | null;
  agent_id: string | null;
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
  output_redacted: boolean;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  approval_id: string | null;
  resume_at: string | null;
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
  /** tool kind -> {config key: allowed values} (apps.automation.agents). */
  tool_scopes: Record<string, Record<string, string[]>>;
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
    next_run_at: str(r.next_run_at),
    url: str(r.webhook_url) ?? str(r.url) ?? str(r.hook_url),
    secret_hint: str(r.secret_hint),
    signature: isObj(r.signature)
      ? { timestamp_header: str(r.signature.timestamp_header), signature_header: str(r.signature.signature_header), algorithm: str(r.signature.algorithm), format: str(r.signature.format) }
      : null,
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
    agent_id: str(r.agent_id),
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
    output_redacted: r.output_redacted === true,
    error: errText(r.error),
    started_at: str(r.started_at),
    finished_at: str(r.finished_at),
    approval_id: str(r.approval_id),
    resume_at: str(r.resume_at),
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
  const tool_scopes: AgentConfig["tool_scopes"] = {};
  if (isObj(r.tool_scopes)) {
    for (const [tool, keys] of Object.entries(r.tool_scopes)) {
      if (!isObj(keys)) continue;
      const entry: Record<string, string[]> = {};
      for (const [k, vals] of Object.entries(keys)) if (Array.isArray(vals)) entry[k] = strings(vals);
      tool_scopes[tool] = entry;
    }
  }
  const rules = r.approval_rules ?? r.approval_required_for;
  return {
    id: str(r.id) ?? "",
    name: str(r.name) ?? "Agent",
    description: str(r.description) ?? "",
    model_tier: str(r.model_tier) ?? "fast",
    allowed_tools: strings(r.allowed_tools ?? r.tools),
    tool_scopes,
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

/** `key = a, b; key2 = c` <-> `{key: [a, b], key2: [c]}` (agent tool scopes). */
export function parseScopes(text: string): { scopes: Record<string, string[]>; error: string | null } {
  const scopes: Record<string, string[]> = {};
  for (const part of text.split(";").map((p) => p.trim()).filter(Boolean)) {
    const eq = part.indexOf("=");
    if (eq < 1) return { scopes: {}, error: `"${part}" should look like key = value1, value2` };
    const key = part.slice(0, eq).trim();
    const values = part.slice(eq + 1).split(",").map((v) => v.trim()).filter(Boolean);
    if (!values.length) return { scopes: {}, error: `${key} needs at least one value` };
    scopes[key] = values;
  }
  return { scopes, error: null };
}

export function formatScopes(scopes: Record<string, string[]> | undefined): string {
  return Object.entries(scopes ?? {})
    .map(([k, v]) => `${k} = ${v.join(", ")}`)
    .join("; ");
}