/**
 * Local (no AI) question suggestions:
 *  - example questions for an empty conversation, derived from the dataset's profiled columns;
 *  - follow-up chips under an answer, derived from the result shape and the remaining columns.
 * They are phrased so the deterministic NL→SQL builder (top/bottom N, measure by dimension, totals, time series,
 * period comparisons) can answer them without Gemini.
 */

import type { DatasetColumn } from "@/lib/types";

const MEASURE_TYPES = /^(big|small)?int|integer|numeric|decimal|double|float|real/i;
const ID_HINT = /(^id$|_id$|^id_|uuid|_key$|code$|zip|pin|phone|postal)/i;

function human(name: string): string {
  return name.replace(/_+/g, " ").trim();
}

export interface ColumnRoles {
  measures: string[];
  dimensions: string[];
  times: string[];
}

export function columnRoles(columns: Array<Pick<DatasetColumn, "name" | "data_type" | "semantic_type" | "is_pii" | "stats">>): ColumnRoles {
  const measures: string[] = [];
  const dimensions: string[] = [];
  const times: string[] = [];
  for (const c of columns) {
    if (c.is_pii) continue;
    const sem = (c.semantic_type ?? "").toLowerCase();
    const type = (c.data_type ?? "").toLowerCase();
    if (/date|time/.test(type) || sem === "date") {
      times.push(c.name);
      continue;
    }
    if (sem === "id" || ID_HINT.test(c.name)) continue;
    if (MEASURE_TYPES.test(type) || sem === "currency" || sem === "percent" || sem === "number") {
      measures.push(c.name);
      continue;
    }
    const distinct = c.stats?.distinct ?? null;
    if (["category", "geo_country", "geo_city", "boolean"].includes(sem) || (distinct !== null && distinct > 0 && distinct <= 200)) {
      dimensions.push(c.name);
    }
  }
  // Prefer money-like measures first (revenue, amount, sales, price).
  measures.sort((a, b) => Number(/revenue|amount|sales|total|price|profit|value/i.test(b)) - Number(/revenue|amount|sales|total|price|profit|value/i.test(a)));
  return { measures, dimensions, times };
}

export function exampleQuestions(
  columns: Array<Pick<DatasetColumn, "name" | "data_type" | "semantic_type" | "is_pii" | "stats">>,
  max = 4,
): string[] {
  const { measures, dimensions, times } = columnRoles(columns);
  const out: string[] = [];
  const m = measures[0];
  const d = dimensions[0];
  const t = times[0];
  if (m && d) out.push(`Top 10 ${human(d)} by total ${human(m)}`);
  if (m && t) out.push(`Total ${human(m)} by month`);
  if (m && t) out.push(`Compare ${human(m)} this month vs last month`);
  if (m && dimensions[1]) out.push(`Average ${human(m)} by ${human(dimensions[1])}`);
  if (d) out.push(`How many rows per ${human(d)}?`);
  if (m) out.push(`What is the total ${human(m)}?`);
  out.push("How many rows are there?");
  return Array.from(new Set(out)).slice(0, max);
}

/** Follow-ups for an answered question, built from the result columns and what the dataset still offers. */
export function followUps(
  question: string,
  resultColumns: string[],
  datasetColumns: Array<Pick<DatasetColumn, "name" | "data_type" | "semantic_type" | "is_pii" | "stats">>,
  max = 3,
): string[] {
  const { measures, dimensions, times } = columnRoles(datasetColumns);
  const used = new Set(resultColumns.map((c) => c.toLowerCase()));
  const q = question.toLowerCase();
  const out: string[] = [];
  const measure = measures.find((m) => used.has(m.toLowerCase()) || q.includes(human(m).toLowerCase())) ?? measures[0];
  const unusedDim = dimensions.find((d) => !used.has(d.toLowerCase()) && !q.includes(human(d).toLowerCase()));
  const time = times[0];
  if (measure && time && !used.has(time.toLowerCase()) && !/month|week|day|year|quarter|trend/.test(q)) {
    out.push(`Show ${human(measure)} by month`);
  }
  if (measure && unusedDim) out.push(`Break ${human(measure)} down by ${human(unusedDim)}`);
  if (/top|highest|most/.test(q)) out.push(q.replace(/\btop\b/, "bottom").replace(/highest|most/, "lowest").replace(/^./, (c) => c.toUpperCase()));
  else if (measure && dimensions[0]) out.push(`Top 5 ${human(dimensions[0])} by ${human(measure)}`);
  if (measure && time && !/vs|compare/.test(q)) out.push(`Compare ${human(measure)} this quarter vs last quarter`);
  return Array.from(new Set(out.filter((s) => s.toLowerCase() !== q))).slice(0, max);
}
