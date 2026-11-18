/**
 * Chart hint → renderable chart spec.
 *
 * The backend sends a `chart` hint ({type, x, y, series}) with each result. The hint is advisory: it is checked
 * against the actual columns (masked or missing columns are dropped, non-numeric measures rejected) and falls back
 * to an inferred chart, and finally to a plain table, so a bad hint never renders a broken chart.
 */

import type { ChartHint, ChartType, ResultColumn } from "@/lib/types";

export interface NormalizedColumn {
  name: string;
  type: string;
  masked: boolean;
  kind: "number" | "temporal" | "text" | "boolean" | "other";
}

export interface ChartSpec {
  type: ChartType;
  x: string | null;
  y: string[];
  series: string | null;
  /** Rows as objects keyed by column name (series pivoted wide when `series` is set). */
  data: Array<Record<string, unknown>>;
  /** Value keys to plot (after a series pivot these are the series values). */
  keys: string[];
  /** Why we fell back / changed the hinted chart (shown as a hint under the chart). */
  note: string | null;
}

const NUMERIC_TYPE = /^(big|small)?int|integer|numeric|decimal|double|float|real|number|money/i;
const TEMPORAL_TYPE = /date|time/i;

export function normalizeColumns(columns: Array<ResultColumn | string>, rows: unknown[][] = []): NormalizedColumn[] {
  return columns.map((c, i) => {
    const col = typeof c === "string" ? { name: c } : c;
    const type = (col.type ?? "").toLowerCase();
    let kind: NormalizedColumn["kind"] = "other";
    if (NUMERIC_TYPE.test(type)) kind = "number";
    else if (TEMPORAL_TYPE.test(type)) kind = "temporal";
    else if (/bool/.test(type)) kind = "boolean";
    else if (/text|char|string|uuid|json/.test(type)) kind = "text";
    else kind = sniffKind(rows.map((r) => r[i]));
    return { name: col.name, type: col.type ?? "", masked: !!col.masked, kind };
  });
}

function sniffKind(values: unknown[]): NormalizedColumn["kind"] {
  const sample = values.filter((v) => v !== null && v !== undefined && v !== "").slice(0, 50);
  if (sample.length === 0) return "other";
  if (sample.every((v) => typeof v === "number" || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v)))) return "number";
  if (sample.every((v) => typeof v === "boolean")) return "boolean";
  if (sample.every((v) => typeof v === "string" && /^\d{4}-\d{2}(-\d{2})?([T ][\d:.]+(Z|[+-]\d{2}:?\d{2})?)?$/.test(v))) return "temporal";
  return "text";
}

export function toNumber(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  return null;
}

function rowsToObjects(cols: NormalizedColumn[], rows: unknown[][]): Array<Record<string, unknown>> {
  return rows.map((r) => {
    const o: Record<string, unknown> = {};
    cols.forEach((c, i) => {
      o[c.name] = c.kind === "number" ? toNumber(r[i]) : r[i];
    });
    return o;
  });
}

function asList(y: ChartHint["y"]): string[] {
  if (!y) return [];
  return Array.isArray(y) ? y : [y];
}

/** Long → wide: one row per x value, one key per series value. Keeps x order of first appearance. */
export function pivotSeries(
  data: Array<Record<string, unknown>>,
  x: string,
  series: string,
  y: string,
  maxSeries = 8,
): { data: Array<Record<string, unknown>>; keys: string[] } {
  const totals = new Map<string, number>();
  for (const r of data) {
    const s = String(r[series] ?? "—");
    totals.set(s, (totals.get(s) ?? 0) + Math.abs(toNumber(r[y]) ?? 0));
  }
  const keys = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k);
  const kept = new Set(keys.slice(0, maxSeries));
  const other = keys.length > maxSeries;
  const byX = new Map<string, Record<string, unknown>>();
  for (const r of data) {
    const xv = String(r[x] ?? "—");
    const row = byX.get(xv) ?? { [x]: r[x] };
    const s = String(r[series] ?? "—");
    const key = kept.has(s) ? s : "Other";
    row[key] = ((row[key] as number | undefined) ?? 0) + (toNumber(r[y]) ?? 0);
    byX.set(xv, row);
  }
  return { data: [...byX.values()], keys: other ? [...keys.slice(0, maxSeries), "Other"] : keys };
}

/** Chart types that make sense for this result (drives the type switcher). */
export function availableChartTypes(columns: NormalizedColumn[], rowCount: number): ChartType[] {
  const visible = columns.filter((c) => !c.masked);
  const nums = visible.filter((c) => c.kind === "number");
  const dims = visible.filter((c) => c.kind !== "number");
  const out: ChartType[] = [];
  if (rowCount === 1 && nums.length >= 1) out.push("kpi");
  if (nums.length >= 1 && rowCount >= 1 && (dims.length >= 1 || nums.length >= 2)) {
    out.push("bar", "line", "area");
    if (rowCount <= 12 && dims.length >= 1) out.push("pie");
  }
  if (nums.length >= 2 && rowCount >= 2) out.push("scatter");
  out.push("table");
  return out;
}

function inferType(columns: NormalizedColumn[], rowCount: number): ChartType {
  const visible = columns.filter((c) => !c.masked);
  const nums = visible.filter((c) => c.kind === "number");
  const temporal = visible.find((c) => c.kind === "temporal");
  if (rowCount === 1 && nums.length >= 1 && visible.length <= 3) return "kpi";
  if (nums.length === 0 || rowCount === 0) return "table";
  if (temporal) return "line";
  if (visible.some((c) => c.kind !== "number")) return "bar";
  return nums.length >= 2 ? "scatter" : "table";
}

/**
 * Resolve a hint (or `override` from the type switcher) into a renderable spec.
 */
export function resolveChart(
  hint: ChartHint | null | undefined,
  columnsIn: Array<ResultColumn | string>,
  rows: unknown[][],
  override?: ChartType | null,
): ChartSpec {
  const columns = normalizeColumns(columnsIn, rows);
  const byName = new Map(columns.map((c) => [c.name, c]));
  const usable = (name: string | null | undefined) => (name ? byName.get(name) : undefined);
  const visible = columns.filter((c) => !c.masked);
  const available = availableChartTypes(columns, rows.length);
  const notes: string[] = [];

  let type: ChartType = (override ?? (hint?.type as ChartType | undefined) ?? inferType(columns, rows.length)) as ChartType;
  if (!available.includes(type)) {
    if (!override && hint?.type) notes.push(`A ${hint.type} chart doesn't fit this result.`);
    type = inferType(columns, rows.length);
    if (!available.includes(type)) type = "table";
  }

  const empty: ChartSpec = { type: "table", x: null, y: [], series: null, data: rowsToObjects(columns, rows), keys: [], note: null };
  if (type === "table") return { ...empty, note: notes[0] ?? null };

  // Measures: hinted y columns that exist, are visible and numeric; else every visible numeric column.
  const hintedY = asList(hint?.y);
  let y = hintedY.filter((n) => usable(n) && !usable(n)!.masked && usable(n)!.kind === "number");
  if (hintedY.some((n) => usable(n)?.masked)) notes.push("Masked columns are not charted.");
  const nums = visible.filter((c) => c.kind === "number");

  // Dimension: hinted x if usable; else first temporal, else first text-like column.
  let x = usable(hint?.x) && !usable(hint?.x)!.masked ? hint!.x! : null;
  if (!x) x = (visible.find((c) => c.kind === "temporal") ?? visible.find((c) => c.kind !== "number"))?.name ?? null;
  if (y.length === 0) y = nums.filter((c) => c.name !== x).map((c) => c.name);

  if (type === "kpi") {
    const metric = y[0] ?? nums[0]?.name;
    return { type: "kpi", x: null, y: metric ? [metric] : [], series: null, data: rowsToObjects(columns, rows), keys: metric ? [metric] : [], note: notes[0] ?? null };
  }
  if (type === "scatter") {
    const [a, b] = nums.map((c) => c.name);
    if (!a || !b) return { ...empty, note: "Scatter needs two numeric columns." };
    return { type, x: a, y: [b], series: null, data: rowsToObjects(columns, rows), keys: [b], note: notes[0] ?? null };
  }
  if (!x) {
    // Numeric-only multi-column result: plot the measures per row index.
    x = "__row";
  }
  if (y.length === 0) return { ...empty, note: "No numeric column to plot." };

  let data = rowsToObjects(columns, rows);
  if (x === "__row") data = data.map((r, i) => ({ ...r, __row: i + 1 }));
  let keys = y;
  const series = usable(hint?.series) && !usable(hint?.series)!.masked && hint!.series !== x ? hint!.series! : null;
  if (series && type !== "pie") {
    const p = pivotSeries(data, x, series, y[0]);
    data = p.data;
    keys = p.keys;
  }
  if (type === "pie") {
    keys = [y[0]];
    if (data.some((r) => (toNumber(r[y[0]]) ?? 0) < 0)) {
      return { ...resolveChart(hint, columnsIn, rows, "bar"), note: "Pie charts can't show negative values, so this is a bar chart." };
    }
  }
  if (type === "line" || type === "area") {
    const xcol = byName.get(x);
    if (xcol?.kind === "temporal") data = [...data].sort((a, b) => String(a[x!]).localeCompare(String(b[x!])));
  }
  return { type, x, y, series: type === "pie" ? null : series, data, keys, note: notes[0] ?? null };
}
