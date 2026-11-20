/**
 * Widget rendering helpers (pure): which spec a widget renders from its live data, and how a failed data
 * request is explained. Widget data always comes from `GET /dashboards/{id}/widgets/{wid}/data`, which re-runs
 * the saved query under the viewer's policy; nothing here invents values.
 */

import { ApiError } from "@/lib/api";
import { normalizeColumns, resolveChart, toNumber, type ChartSpec } from "@/lib/chat/chart";
import { guardMessage, isGuardCode } from "@/lib/chat/error-copy";
import type { ChartHint, ChartType, DashboardWidget, WidgetData, WidgetKind } from "@/lib/types";

export const WIDGET_KINDS: Array<{ value: WidgetKind; label: string; description: string }> = [
  { value: "chart", label: "Chart", description: "Bar, line, area or pie chart of the result" },
  { value: "table", label: "Table", description: "The result rows" },
  { value: "kpi", label: "KPI", description: "One headline number" },
  { value: "text", label: "Text", description: "A note or heading (no query)" },
];

export interface KpiView {
  label: string;
  value: number | string | null;
  caption: string | null;
}

/** The KPI value: the first numeric column of the first row (hint `y` wins when it is numeric). */
export function kpiFromData(data: Pick<WidgetData, "columns" | "rows">, hint?: ChartHint | null): KpiView {
  const cols = normalizeColumns(data.columns, data.rows);
  const row = data.rows[0] ?? [];
  const hinted = Array.isArray(hint?.y) ? hint?.y[0] : hint?.y;
  let idx = hinted ? cols.findIndex((c) => c.name === hinted && c.kind === "number" && !c.masked) : -1;
  if (idx < 0) idx = cols.findIndex((c) => c.kind === "number" && !c.masked);
  if (idx < 0) idx = cols.findIndex((c) => !c.masked);
  if (idx < 0 || data.rows.length === 0) return { label: cols[0]?.name ?? "Value", value: null, caption: data.rows.length === 0 ? "No rows" : null };
  const raw = row[idx];
  const num = toNumber(raw);
  const others = cols
    .map((c, i) => ({ c, i }))
    .filter(({ c, i }) => i !== idx && !c.masked)
    .slice(0, 2)
    .map(({ c, i }) => `${c.name}: ${row[i] ?? "—"}`);
  return { label: cols[idx].name, value: num ?? (raw === null || raw === undefined ? null : String(raw)), caption: others.length ? others.join(" · ") : null };
}

/** Chart spec for a chart widget: the widget's own chart config wins over the data's hint. */
export function chartSpecFor(widget: Pick<DashboardWidget, "chart">, data: WidgetData, override?: ChartType | null): ChartSpec {
  return resolveChart(widget.chart ?? data.chart ?? null, data.columns, data.rows, override ?? null);
}

export type WidgetErrorKind = "permission" | "missing" | "rejected" | "unavailable" | "error";

/** Explain a failed `/data` request. 403/404 mean the viewer may not query the widget's dataset. */
export function widgetErrorState(err: unknown): { kind: WidgetErrorKind; title: string; description: string } {
  if (!(err instanceof ApiError)) return { kind: "error", title: "Couldn't load this widget", description: "An unexpected error occurred." };
  if (err.status === 403 || err.status === 404) {
    return {
      kind: "permission",
      title: "No access to this data",
      description: "You don't have access to the dataset behind this widget, so it is not shown.",
    };
  }
  if (err.code === "widget_query_missing") return { kind: "missing", title: "Query deleted", description: "The saved query behind this widget was deleted." };
  if (isGuardCode(err.code) || err.status === 422) {
    return {
      kind: "rejected",
      title: "Query rejected under your policy",
      description: guardMessage(err.code) ?? err.detail ?? "The SQL guard rejected this query for your access policy.",
    };
  }
  if (err.status === 0 || err.status === 502 || err.status === 503 || err.status === 504) {
    return { kind: "unavailable", title: "Data service unavailable", description: "The warehouse did not respond. Refresh to try again." };
  }
  return { kind: "error", title: "Couldn't load this widget", description: err.detail ?? err.message };
}
