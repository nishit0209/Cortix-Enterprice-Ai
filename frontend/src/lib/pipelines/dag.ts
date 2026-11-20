/**
 * Pure pipeline DAG helpers: tolerant parsing, serialization, validation, static schema inference and layout.
 *
 * This is the builder's own model ({ nodes: [{ id, type, config, position }], edges: [{ from, to }] }).
 * `wire.ts` converts it to and from the backend DAG (`source.dataset`, `destination.dataset`, backend config
 * field names). The parser also accepts `key` instead of `id`, `[from, to]` / `{source, target}` edges and the
 * dotted type names of DATA_ENGINEERING_DESIGN §5.2.
 */

import type { PipelineDagNode } from "@/lib/types";
import { canonicalType, LIST_OPS, STEP_BY_TYPE } from "./catalog";
import { expressionColumns, tryParseExpression } from "./expr";

export interface DagEdge {
  from: string;
  to: string;
  port?: "left" | "right";
}

export interface BuilderDag {
  nodes: PipelineDagNode[];
  edges: DagEdge[];
  settings?: Record<string, unknown>;
}

/* ---------------- parsing ---------------- */

export function parseDag(raw: unknown): BuilderDag {
  const obj = (raw && typeof raw === "object" ? raw : {}) as { nodes?: unknown; edges?: unknown; settings?: unknown };
  const nodes: PipelineDagNode[] = [];
  for (const n of Array.isArray(obj.nodes) ? obj.nodes : []) {
    if (!n || typeof n !== "object") continue;
    const r = n as Record<string, unknown>;
    const id = String(r.id ?? r.key ?? "");
    if (!id) continue;
    const pos = r.position as { x?: unknown; y?: unknown } | undefined;
    nodes.push({
      id,
      type: canonicalType(String(r.type ?? "")),
      config: (r.config && typeof r.config === "object" ? r.config : {}) as Record<string, unknown>,
      position: pos && typeof pos.x === "number" && typeof pos.y === "number" ? { x: pos.x, y: pos.y } : undefined,
      label: typeof r.label === "string" ? r.label : undefined,
    });
  }
  const edges: DagEdge[] = [];
  for (const e of Array.isArray(obj.edges) ? obj.edges : []) {
    if (Array.isArray(e) && e.length >= 2) edges.push({ from: String(e[0]), to: String(e[1]) });
    else if (e && typeof e === "object") {
      const r = e as Record<string, unknown>;
      const from = r.from ?? r.source;
      const to = r.to ?? r.target;
      const port = r.port ?? r.target_handle ?? r.targetHandle;
      if (from && to) edges.push({ from: String(from), to: String(to), ...(port === "left" || port === "right" ? { port } : {}) });
    }
  }
  const dag: BuilderDag = { nodes, edges };
  if (obj.settings && typeof obj.settings === "object") dag.settings = obj.settings as Record<string, unknown>;
  if (nodes.some((n) => !n.position)) return withLayout(dag);
  return dag;
}

/** Canonical builder snapshot (stable key order, rounded positions, undefined config values dropped). Used for dirty checks. */
export function serializeDag(dag: BuilderDag): BuilderDag {
  return {
    nodes: dag.nodes.map((n) => ({
      id: n.id,
      type: n.type,
      config: cleanConfig(n.config),
      ...(n.position ? { position: { x: Math.round(n.position.x), y: Math.round(n.position.y) } } : {}),
    })),
    edges: dag.edges.map((e) => ({ from: e.from, to: e.to, ...(e.port ? { port: e.port } : {}) })),
    ...(dag.settings ? { settings: dag.settings } : {}),
  };
}

function cleanConfig(c: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(c)) {
    if (v === undefined) continue;
    out[k] = v;
  }
  return out;
}

/* ---------------- graph utilities ---------------- */

export function topoOrder(nodeIds: string[], edges: DagEdge[]): { order: string[]; cyclic: string[] } {
  const indeg = new Map(nodeIds.map((id) => [id, 0]));
  const out = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  for (const e of edges) {
    if (!indeg.has(e.from) || !indeg.has(e.to)) continue;
    indeg.set(e.to, indeg.get(e.to)! + 1);
    out.get(e.from)!.push(e.to);
  }
  const queue = nodeIds.filter((id) => indeg.get(id) === 0);
  const order: string[] = [];
  while (queue.length) {
    const id = queue.shift()!;
    order.push(id);
    for (const t of out.get(id)!) {
      indeg.set(t, indeg.get(t)! - 1);
      if (indeg.get(t) === 0) queue.push(t);
    }
  }
  const done = new Set(order);
  return { order, cyclic: nodeIds.filter((id) => !done.has(id)) };
}

/** Longest-path layering → left-to-right positions (used when a DAG or lineage graph has no layout). */
export function layeredLayout(
  nodeIds: string[],
  edges: Array<{ from: string; to: string }>,
  { dx = 280, dy = 110 }: { dx?: number; dy?: number } = {},
): Record<string, { x: number; y: number }> {
  const { order, cyclic } = topoOrder(nodeIds, edges);
  const layer = new Map<string, number>();
  for (const id of [...order, ...cyclic]) {
    const preds = edges.filter((e) => e.to === id && layer.has(e.from));
    layer.set(id, preds.length ? Math.max(...preds.map((e) => layer.get(e.from)! + 1)) : 0);
  }
  const columns = new Map<number, string[]>();
  for (const id of [...order, ...cyclic]) {
    const l = layer.get(id)!;
    columns.set(l, [...(columns.get(l) ?? []), id]);
  }
  const tallest = Math.max(1, ...[...columns.values()].map((c) => c.length));
  const pos: Record<string, { x: number; y: number }> = {};
  for (const [l, ids] of columns) {
    const offset = ((tallest - ids.length) * dy) / 2;
    ids.forEach((id, i) => (pos[id] = { x: l * dx, y: offset + i * dy }));
  }
  return pos;
}

export function withLayout(dag: BuilderDag): BuilderDag {
  const pos = layeredLayout(
    dag.nodes.map((n) => n.id),
    dag.edges,
  );
  return { ...dag, nodes: dag.nodes.map((n) => ({ ...n, position: pos[n.id] ?? { x: 0, y: 0 } })) };
}

/* ---------------- schema inference ---------------- */

export interface SchemaColumn {
  name: string;
  type: string | null;
}

/**
 * Static output schema per node. `sourceColumns` maps a source node's dataset id to its columns.
 * Returns null for a node whose input schema is unknown.
 */
export function inferSchemas(
  dag: BuilderDag,
  sourceColumns: Record<string, SchemaColumn[] | undefined>,
): Record<string, SchemaColumn[] | null> {
  const { order } = topoOrder(
    dag.nodes.map((n) => n.id),
    dag.edges,
  );
  const byId = new Map(dag.nodes.map((n) => [n.id, n]));
  const out: Record<string, SchemaColumn[] | null> = {};
  for (const id of order) {
    const n = byId.get(id)!;
    const ins = dag.edges.filter((e) => e.to === id);
    const left = ins.find((e) => e.port === "left") ?? ins[0];
    const right = ins.find((e) => e !== left);
    const input = left ? (out[left.from] ?? null) : null;
    const c = n.config;
    const cols = (k: string) => (Array.isArray(c[k]) ? (c[k] as string[]) : []);
    switch (n.type) {
      case "source":
        out[id] = sourceColumns[String(c.dataset_id ?? "")] ?? null;
        break;
      case "select_columns":
        out[id] = input ? input.filter((col) => cols("columns").includes(col.name)) : null;
        break;
      case "rename": {
        const m = (c.mapping ?? {}) as Record<string, string>;
        out[id] = input ? input.map((col) => ({ ...col, name: m[col.name] || col.name })) : null;
        break;
      }
      case "cast": {
        const casts = (Array.isArray(c.casts) ? c.casts : []) as Array<{ column: string; to: string }>;
        out[id] = input ? input.map((col) => ({ ...col, type: casts.find((x) => x.column === col.name)?.to ?? col.type })) : null;
        break;
      }
      case "derive":
        out[id] = input ? [...input.filter((col) => col.name !== c.name), { name: String(c.name || "derived"), type: null }] : null;
        break;
      case "aggregate": {
        const aggs = (Array.isArray(c.aggregations) ? c.aggregations : []) as Array<{ column: string; fn: string; as?: string }>;
        const aggType = (a: { column: string; fn: string }) => {
          if (a.fn.startsWith("count")) return "bigint";
          if (["min", "max", "first", "last"].includes(a.fn)) return input?.find((x) => x.name === a.column)?.type ?? null;
          return "double precision";
        };
        out[id] = input
          ? [
              ...input.filter((col) => cols("group_by").includes(col.name)),
              ...aggs.map((a) => ({ name: a.as || (a.column && a.column !== "*" ? `${a.column}_${a.fn}` : "row_count"), type: aggType(a) })),
            ]
          : null;
        break;
      }
      case "join": {
        // The joined dataset comes from the join's config (backend JoinCfg.dataset_id), not from a second edge.
        const r = c.dataset_id ? (sourceColumns[String(c.dataset_id)] ?? null) : right ? (out[right.from] ?? null) : null;
        if (!input || !r) {
          out[id] = null;
          break;
        }
        const names = new Set(input.map((col) => col.name));
        const keys = new Set(((Array.isArray(c.on) ? c.on : []) as Array<{ left: string; right: string }>).filter((k) => k.left === k.right).map((k) => k.right));
        out[id] = [...input, ...r.filter((col) => !keys.has(col.name)).map((col) => (names.has(col.name) ? { ...col, name: `${col.name}_right` } : col))];
        break;
      }
      default:
        out[id] = input;
    }
  }
  for (const n of dag.nodes) if (!(n.id in out)) out[n.id] = null;
  return out;
}

/* ---------------- validation ---------------- */

export interface DagIssue {
  nodeId: string | null;
  message: string;
}

export interface DagValidation {
  ok: boolean;
  issues: DagIssue[];
  byNode: Record<string, string[]>;
}

function referencedColumns(type: string, c: Record<string, unknown>): Array<{ col: string; side?: "left" | "right" }> {
  if (type === "derive" && typeof c.expression === "string") {
    const parsed = tryParseExpression(c.expression);
    return parsed.ok ? expressionColumns(parsed.tree).map((col) => ({ col })) : [];
  }
  const list = (k: string) => (Array.isArray(c[k]) ? (c[k] as unknown[]).map(String) : []);
  switch (type) {
    case "select_columns":
    case "trim_whitespace":
    case "change_case":
    case "fill_null":
    case "drop_nulls":
      return list("columns").map((col) => ({ col }));
    case "deduplicate":
      return list("keys").map((col) => ({ col }));
    case "rename":
      return Object.keys((c.mapping ?? {}) as object).map((col) => ({ col }));
    case "cast":
      return ((c.casts ?? []) as Array<{ column: string }>).map((x) => ({ col: x.column }));
    case "filter":
      return ((c.conditions ?? []) as Array<{ column: string }>).map((x) => ({ col: x.column }));
    case "sort":
      return ((c.by ?? []) as Array<{ column: string }>).map((x) => ({ col: x.column }));
    case "aggregate":
      return [
        ...list("group_by").map((col) => ({ col })),
        ...((c.aggregations ?? []) as Array<{ column: string; fn: string }>).filter((a) => a.column && a.column !== "*").map((a) => ({ col: a.column })),
      ];
    case "join":
      return ((c.on ?? []) as Array<{ left: string; right: string }>).flatMap((k) => [
        { col: k.left, side: "left" as const },
        { col: k.right, side: "right" as const },
      ]);
    default:
      return [];
  }
}

function isBlank(v: unknown): boolean {
  if (v === undefined || v === null) return true;
  if (typeof v === "string") return v.trim() === "";
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}

const NAME_RE = /^[a-z_][a-z0-9_]{0,62}$/;

/**
 * Client-side validation mirroring the backend rules (linear chain, required fields, column references).
 * `datasetColumns` (dataset id -> columns) resolves the right-hand side of joins.
 */
export function validateDag(
  dag: BuilderDag,
  schemas?: Record<string, SchemaColumn[] | null>,
  datasetColumns?: Record<string, SchemaColumn[] | undefined>,
): DagValidation {
  const issues: DagIssue[] = [];
  const add = (nodeId: string | null, message: string) => issues.push({ nodeId, message });
  const ids = new Set<string>();
  for (const n of dag.nodes) {
    if (ids.has(n.id)) add(n.id, `Duplicate node id "${n.id}".`);
    ids.add(n.id);
  }
  const sources = dag.nodes.filter((n) => n.type === "source");
  const dests = dag.nodes.filter((n) => n.type === "destination");
  if (dag.nodes.length === 0) add(null, "Add a source, at least one step and a destination.");
  else {
    if (sources.length === 0) add(null, "The pipeline needs a source.");
    if (dests.length === 0) add(null, "The pipeline needs a dataset output.");
    if (dests.length > 1) dests.slice(1).forEach((d) => add(d.id, "Only one output is allowed per pipeline."));
    if (sources.length > 1) sources.slice(1).forEach((d) => add(d.id, "Only one source is allowed. Use a Join step to bring in another dataset."));
  }

  const seen = new Set<string>();
  for (const e of dag.edges) {
    if (!ids.has(e.from) || !ids.has(e.to)) add(null, `An edge points to a missing node (${e.from} → ${e.to}).`);
    if (e.from === e.to) add(e.from, "A step cannot connect to itself.");
    const k = `${e.from}->${e.to}`;
    if (seen.has(k)) add(e.to, "Duplicate connection.");
    seen.add(k);
  }

  const { cyclic } = topoOrder([...ids], dag.edges);
  cyclic.forEach((id) => add(id, "This step is part of a cycle."));

  for (const n of dag.nodes) {
    const spec = STEP_BY_TYPE[n.type];
    if (!spec) {
      add(n.id, `Unknown step type "${n.type}".`);
      continue;
    }
    const ins = dag.edges.filter((e) => e.to === n.id);
    const outs = dag.edges.filter((e) => e.from === n.id);
    if (spec.inputs === 0 && ins.length > 0) add(n.id, "A source cannot have inputs.");
    if (spec.inputs === 1 && ins.length !== 1) add(n.id, ins.length === 0 ? "Connect an input to this step." : "This step takes exactly one input.");
    if (spec.outputs === 0 && outs.length > 0) add(n.id, "The output cannot feed other steps.");
    if (spec.outputs > 0 && outs.length === 0) add(n.id, "Connect this step to a following step.");
    if (outs.length > 1) add(n.id, "Pipelines are a single chain: a step can feed only one next step.");
    for (const f of spec.fields) {
      if (!f.required) continue;
      // Missing keys fall back to the catalog defaults (e.g. an output without `target` writes a new dataset).
      if (f.showIf && !f.showIf.in.includes(String(n.config[f.showIf.key] ?? spec.defaults[f.showIf.key] ?? ""))) continue;
      if (isBlank(n.config[f.key] ?? spec.defaults[f.key])) add(n.id, `${f.label} is required.`);
    }
    if (n.type === "derive" && !isBlank(n.config.name) && !NAME_RE.test(String(n.config.name))) {
      add(n.id, "New column names use lowercase letters, digits and underscores.");
    }
    if (n.type === "rename") {
      const targets = Object.values((n.config.mapping ?? {}) as Record<string, string>);
      if (targets.some((t) => !t || !NAME_RE.test(t))) add(n.id, "New names use lowercase letters, digits and underscores.");
      if (new Set(targets).size !== targets.length) add(n.id, "Two columns are renamed to the same name.");
    }
    if (n.type === "aggregate") {
      const aggs = (n.config.aggregations ?? []) as Array<{ column?: string; fn?: string }>;
      if (aggs.some((a) => !a.fn || (!a.column && a.fn !== "count"))) add(n.id, "Every aggregation needs a function and a column.");
    }
    if (n.type === "join") {
      const keys = (n.config.on ?? []) as Array<{ left?: string; right?: string }>;
      if (keys.some((k) => !k.left || !k.right)) add(n.id, "Every join key needs a left and a right column.");
    }
    if (n.type === "derive" && typeof n.config.expression === "string" && n.config.expression.trim()) {
      const parsed = tryParseExpression(n.config.expression);
      if (!parsed.ok) add(n.id, `Expression: ${parsed.error}`);
    }
    if (n.type === "filter") {
      const conds = (n.config.conditions ?? []) as Array<{ op?: string; value?: unknown }>;
      if (conds.some((x) => x.op === "between" && (!Array.isArray(x.value) || x.value.length !== 2))) add(n.id, "“between” needs two values: low, high.");
      if (conds.some((x) => (x.op === "in" || x.op === "not_in") && (!Array.isArray(x.value) || x.value.length === 0))) add(n.id, "List conditions need at least one value.");
      if (conds.some((x) => x.op && !LIST_OPS.has(x.op) && x.op !== "is_null" && x.op !== "not_null" && isBlank(x.value))) add(n.id, "Every condition needs a value.");
    }
    // Column references must exist in the input schema when it is known.
    if (schemas && ins.length > 0) {
      for (const ref of referencedColumns(n.type, n.config)) {
        const edge = ins.find((e) => e.port === "left") ?? ins[0];
        const schema =
          ref.side === "right"
            ? (datasetColumns?.[String(n.config.dataset_id ?? "")] ?? null)
            : edge
              ? schemas[edge.from]
              : null;
        if (schema && ref.col && !schema.some((c) => c.name === ref.col)) add(n.id, `Column "${ref.col}" is not in the input.`);
      }
    }
  }

  // Reachability: every node must come from a source and lead to the output.
  if (sources.length && dests.length) {
    const down = reach(sources.map((s) => s.id), dag.edges, "down");
    const up = reach(dests.map((d) => d.id), dag.edges, "up");
    for (const n of dag.nodes) {
      if (n.type !== "source" && !down.has(n.id)) add(n.id, "Not reachable from a source.");
      else if (n.type !== "destination" && !up.has(n.id) && dag.edges.some((e) => e.from === n.id)) add(n.id, "Does not lead to the output.");
    }
  }

  const byNode: Record<string, string[]> = {};
  for (const i of issues) if (i.nodeId) byNode[i.nodeId] = Array.from(new Set([...(byNode[i.nodeId] ?? []), i.message]));
  return { ok: issues.length === 0, issues, byNode };
}

function reach(start: string[], edges: DagEdge[], dir: "up" | "down"): Set<string> {
  const seen = new Set(start);
  const stack = [...start];
  while (stack.length) {
    const id = stack.pop()!;
    for (const e of edges) {
      const [a, b] = dir === "down" ? [e.from, e.to] : [e.to, e.from];
      if (a === id && !seen.has(b)) {
        seen.add(b);
        stack.push(b);
      }
    }
  }
  return seen;
}

/** Map server-side validation errors (`errors[].field` like "nodes.s2.config.columns" or "nodes[2]") to node ids. */
export function serverErrorsByNode(errors: Array<{ field: string | null; message: string }>, dag: BuilderDag): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const e of errors) {
    if (!e.field) continue;
    const m = /nodes(?:\.|\[)([^.\]]+)/.exec(e.field);
    if (!m) continue;
    const key = m[1];
    const node = dag.nodes.find((n) => n.id === key) ?? (/^\d+$/.test(key) ? dag.nodes[Number(key)] : undefined);
    if (node) out[node.id] = [...(out[node.id] ?? []), e.message];
  }
  return out;
}

let idSeq = 0;
export function newNodeId(type: string, existing: string[]): string {
  const base = type.replace(/[^a-z]/g, "").slice(0, 8) || "step";
  let id: string;
  do {
    idSeq++;
    id = `${base}_${idSeq}`;
  } while (existing.includes(id));
  return id;
}
