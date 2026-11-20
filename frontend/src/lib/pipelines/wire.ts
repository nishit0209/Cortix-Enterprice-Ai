/**
 * Builder model <-> backend DAG wire format (`backend/apps/pipelines/dag.py` + `steps.py`).
 *
 * The backend accepts a LINEAR chain `source.* -> steps -> destination.dataset`, rejects unknown keys
 * (`extra="forbid"`) and uses its own config field names. The builder keeps friendlier editing shapes
 * (lists of rows, a text formula for `derive`, a "new/existing" switch on the output); this module is the
 * single place that translates between the two, so every save/preview/validate call speaks the backend's language.
 */

import type { PipelineDag, PipelineDagNode } from "@/lib/types";
import { parseDag, type BuilderDag, type SchemaColumn } from "./dag";
import { printExpression, tryParseExpression } from "./expr";

const NUMERIC_TYPES = new Set(["bigint", "double precision", "numeric", "integer", "int", "float", "double", "number", "smallint", "real"]);
const NUMBER_RE = /^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

function asList(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

function strList(v: unknown): string[] {
  return asList(v).map(String).filter(Boolean);
}

/** Coerce a typed-in value to the JSON scalar the backend expects for a column of `type`. */
export function coerceScalar(value: unknown, type: string | null | undefined): unknown {
  if (typeof value !== "string") return value;
  const t = (type ?? "").toLowerCase();
  const v = value.trim();
  if (t === "boolean" && /^(true|false)$/i.test(v)) return v.toLowerCase() === "true";
  if ((t === "" || NUMERIC_TYPES.has(t)) && NUMBER_RE.test(v)) return Number(v);
  return value;
}

const FILTER_OP_ALIASES: Record<string, string> = { "=": "eq", "==": "eq", "!=": "ne", "<>": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte" };
const FILL_ALIASES: Record<string, string> = { constant: "value", forward_fill: "forward", backward_fill: "backward" };
const AGG_ALIASES: Record<string, string> = { avg: "mean", average: "mean", stddev: "std" };

/* ---------------- builder -> wire ---------------- */

function nodeToWire(n: PipelineDagNode, input: SchemaColumn[] | null): { type: string; config: Record<string, unknown> } {
  const c = n.config ?? {};
  const typeOf = (col: string) => input?.find((x) => x.name === col)?.type ?? null;
  switch (n.type) {
    case "source":
      return { type: "source.dataset", config: { dataset_id: c.dataset_id, ...(strList(c.columns).length ? { columns: strList(c.columns) } : {}) } };
    case "destination": {
      const cfg: Record<string, unknown> = c.target === "existing" ? { dataset_id: c.dataset_id } : { name: String(c.dataset_name ?? c.name ?? "").trim() };
      if (c.classification && c.classification !== "internal") cfg.classification = c.classification;
      if (typeof c.description === "string" && c.description.trim()) cfg.description = c.description.trim();
      return { type: "destination.dataset", config: cfg };
    }
    case "select_columns":
      return { type: n.type, config: { columns: strList(c.columns) } };
    case "rename": {
      const mapping: Record<string, string> = {};
      for (const [from, to] of Object.entries((c.mapping ?? {}) as Record<string, unknown>)) if (from && to) mapping[from] = String(to);
      return { type: n.type, config: { mapping } };
    }
    case "cast": {
      const columns: Record<string, string> = {};
      for (const row of asList(c.casts) as Array<{ column?: string; to?: string }>) if (row.column && row.to) columns[row.column] = row.to;
      return { type: n.type, config: { columns, on_error: c.on_error ?? "null" } };
    }
    case "trim_whitespace": {
      const cols = strList(c.columns);
      return { type: n.type, config: cols.length ? { columns: cols } : {} };
    }
    case "change_case":
      return { type: n.type, config: { columns: strList(c.columns), case: c.case ?? "lower" } };
    case "fill_null": {
      const cols = strList(c.columns);
      const strategy = FILL_ALIASES[String(c.strategy)] ?? String(c.strategy ?? "value");
      const cfg: Record<string, unknown> = { columns: cols, strategy };
      if (strategy === "value") {
        // A known text/date column keeps the typed text; numeric (or unknown) columns get a number when it looks like one.
        const known = cols.map(typeOf).filter((t): t is string => !!t);
        const textual = known.find((t) => !NUMERIC_TYPES.has(t.toLowerCase()));
        cfg.value = coerceScalar(c.value ?? null, textual ?? known[0] ?? null);
      }
      return { type: n.type, config: cfg };
    }
    case "drop_nulls": {
      const cols = strList(c.columns);
      return { type: n.type, config: { ...(cols.length ? { columns: cols } : {}), how: c.how === "all" ? "all" : "any" } };
    }
    case "deduplicate": {
      const subset = strList(c.subset ?? c.keys);
      return { type: n.type, config: { ...(subset.length ? { subset } : {}), keep: c.keep === "last" ? "last" : "first" } };
    }
    case "filter": {
      const conditions = (asList(c.conditions) as Array<{ column?: string; op?: string; value?: unknown; case_sensitive?: boolean }>).map((x) => {
        const op = FILTER_OP_ALIASES[String(x.op)] ?? String(x.op ?? "eq");
        const t = typeOf(String(x.column ?? ""));
        const cond: Record<string, unknown> = { column: x.column ?? "", op };
        if (op === "is_null" || op === "not_null") return cond;
        if (op === "contains" || op === "starts_with" || op === "ends_with") cond.value = String(x.value ?? "");
        else if (op === "in" || op === "not_in" || op === "between") cond.value = asList(x.value).map((v) => coerceScalar(v, t));
        else cond.value = coerceScalar(x.value ?? "", t);
        if (x.case_sensitive === false) cond.case_sensitive = false;
        return cond;
      });
      return { type: n.type, config: { conditions, match: (c.match ?? c.mode) === "any" ? "any" : "all" } };
    }
    case "derive": {
      const parsed = typeof c.expression === "string" ? tryParseExpression(c.expression) : null;
      // An unparsable formula is sent as-is; the server reports it on this node's `expression` field.
      const expression = parsed ? (parsed.ok ? parsed.tree : c.expression) : c.expression;
      return { type: n.type, config: { name: String(c.name ?? "").trim(), expression, ...(c.replace ? { replace: true } : {}) } };
    }
    case "sort":
      return {
        type: n.type,
        config: { by: (asList(c.by) as Array<{ column?: string; desc?: boolean; descending?: boolean }>).map((s) => ({ column: s.column ?? "", descending: !!(s.descending ?? s.desc) })) },
      };
    case "aggregate":
      return {
        type: n.type,
        config: {
          group_by: strList(c.group_by),
          aggregations: (asList(c.aggregations) as Array<{ column?: string; fn?: string; func?: string; as?: string; alias?: string }>).map((a) => {
            const func = AGG_ALIASES[String(a.fn ?? a.func)] ?? String(a.fn ?? a.func ?? "count");
            const col = a.column && a.column !== "*" ? a.column : null;
            const alias = (a.as ?? a.alias ?? "").trim();
            return { func, ...(col ? { column: col } : {}), ...(alias ? { alias } : {}) };
          }),
        },
      };
    case "join":
      return {
        type: n.type,
        config: {
          dataset_id: c.dataset_id,
          how: c.how === "left" ? "left" : "inner",
          on: (asList(c.on) as Array<{ left?: string; right?: string }>).map((k) => ({ left: k.left ?? "", right: k.right ?? "" })),
          ...(strList(c.columns).length ? { columns: strList(c.columns) } : {}),
        },
      };
    default:
      return { type: n.type, config: { ...c } };
  }
}

/**
 * Builder DAG -> backend wire DAG. `schemas` (static inference per node) lets filter/fill values be sent with the
 * right JSON type for their column. Edge ports and builder-only settings are dropped.
 */
export function toWireDag(dag: BuilderDag, schemas?: Record<string, SchemaColumn[] | null>): PipelineDag {
  return {
    nodes: dag.nodes.map((n) => {
      const inEdge = dag.edges.find((e) => e.to === n.id);
      const input = inEdge && schemas ? (schemas[inEdge.from] ?? null) : null;
      const { type, config } = nodeToWire(n, input);
      return {
        id: n.id,
        type,
        config,
        ...(n.label ? { label: n.label.slice(0, 120) } : {}),
        ...(n.position ? { position: { x: Math.round(n.position.x), y: Math.round(n.position.y) } } : {}),
      };
    }),
    edges: dag.edges.map((e) => ({ from: e.from, to: e.to })),
  };
}

/* ---------------- wire -> builder ---------------- */

function nodeFromWire(type: string, c: Record<string, unknown>): Record<string, unknown> {
  switch (type) {
    case "source.dataset":
    case "source":
      return { dataset_id: c.dataset_id ?? "", ...(c.columns ? { columns: c.columns } : {}) };
    case "destination.dataset":
    case "destination":
      return c.dataset_id
        ? { target: "existing", dataset_id: c.dataset_id, dataset_name: "", classification: c.classification ?? "internal" }
        : { target: "new", dataset_name: c.name ?? c.dataset_name ?? "", classification: c.classification ?? "internal", ...(c.description ? { description: c.description } : {}) };
    case "cast": {
      if (Array.isArray(c.casts)) return c;
      const columns = (c.columns ?? {}) as Record<string, string>;
      return { casts: Object.entries(columns).map(([column, to]) => ({ column, to })), on_error: c.on_error ?? "null" };
    }
    case "trim_whitespace":
    case "drop_nulls":
      return { ...c, columns: strList(c.columns) };
    case "fill_null":
      return { ...c, strategy: FILL_ALIASES[String(c.strategy)] ?? c.strategy ?? "value", value: c.value === null || c.value === undefined ? "" : String(c.value) };
    case "deduplicate":
      return { keys: strList(c.subset ?? c.keys), keep: c.keep ?? "first" };
    case "filter":
      return {
        mode: c.match ?? c.mode ?? "all",
        conditions: (asList(c.conditions) as Array<Record<string, unknown>>).map((x) => ({
          ...x,
          op: FILTER_OP_ALIASES[String(x.op)] ?? x.op,
          value: Array.isArray(x.value) ? x.value.map(String) : x.value === null || x.value === undefined ? "" : String(x.value),
        })),
      };
    case "derive":
      return { ...c, expression: typeof c.expression === "string" ? c.expression : c.expression ? printExpression(c.expression) : "" };
    case "sort":
      return { by: (asList(c.by) as Array<Record<string, unknown>>).map((s) => ({ column: s.column, desc: !!(s.descending ?? s.desc) })) };
    case "aggregate":
      return {
        group_by: strList(c.group_by),
        aggregations: (asList(c.aggregations) as Array<Record<string, unknown>>).map((a) => ({
          fn: AGG_ALIASES[String(a.func ?? a.fn)] ?? a.func ?? a.fn ?? "count",
          column: a.column ?? "*",
          as: a.alias ?? a.as ?? "",
        })),
      };
    default:
      return c;
  }
}

/** Backend DAG -> builder DAG (positions are laid out when missing). */
export function fromWireDag(raw: unknown): BuilderDag {
  const obj = (raw && typeof raw === "object" ? raw : {}) as { nodes?: unknown; edges?: unknown };
  const nodes = (Array.isArray(obj.nodes) ? obj.nodes : []).map((n) => {
    if (!n || typeof n !== "object") return n;
    const r = n as Record<string, unknown>;
    const cfg = (r.config && typeof r.config === "object" ? r.config : {}) as Record<string, unknown>;
    return { ...r, config: nodeFromWire(String(r.type ?? ""), cfg) };
  });
  return parseDag({ nodes, edges: obj.edges });
}

/* ---------------- step preview ---------------- */

/**
 * The DAG to preview the output of `nodeId`: the chain from the source up to that node, followed by the
 * destination (the backend requires one). Returns the full DAG when the node is the destination or unknown.
 */
export function dagUpTo(dag: BuilderDag, nodeId: string | null): BuilderDag {
  if (!nodeId) return dag;
  const target = dag.nodes.find((n) => n.id === nodeId);
  const dest = dag.nodes.find((n) => n.type === "destination");
  if (!target || !dest || target.id === dest.id) return dag;
  const keep = new Set<string>([target.id]);
  let cur = target.id;
  for (let guard = 0; guard < dag.nodes.length; guard++) {
    const inEdge = dag.edges.find((e) => e.to === cur);
    if (!inEdge || keep.has(inEdge.from)) break;
    keep.add(inEdge.from);
    cur = inEdge.from;
  }
  return {
    ...dag,
    nodes: [...dag.nodes.filter((n) => keep.has(n.id)), dest],
    edges: [...dag.edges.filter((e) => keep.has(e.from) && keep.has(e.to)), { from: target.id, to: dest.id }],
  };
}
