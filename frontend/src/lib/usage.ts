/**
 * Org usage (GET /orgs/{org}/usage, `apps/bi/usage.py`) + AI usage (GET /orgs/{org}/ai/usage) helpers. Pure; unit-tested.
 *
 * The usage API takes `from`/`to` as UTC dates (YYYY-MM-DD, at most 366 days). A metric whose source app is missing
 * answers `{available: false}`; the UI then says so instead of showing zeros.
 */

import type { AiUsage } from "@/lib/types";

export const USAGE_METRICS = ["queries", "active_users", "datasets", "storage", "ai", "pipeline_runs", "documents"] as const;
export type UsageMetric = (typeof USAGE_METRICS)[number];
export const MAX_USAGE_DAYS = 366;

export interface DayCount {
  day: string;
  count: number;
}

export interface UsageReport {
  range: { from: string; to: string; days: number; timezone: string };
  generated_at: string;
  metrics: Partial<{
    queries: Unavailable | { total: number; by_verdict: Record<string, number>; by_purpose: Record<string, number>; per_day: DayCount[]; per_user?: Array<{ user_id: string; email: string | null; count: number }> };
    active_users: Unavailable | { distinct: number; members_active: number; per_day: DayCount[] };
    datasets: Unavailable | { total: number; by_status: Record<string, number>; created: number; per_day: DayCount[] };
    storage: Unavailable | { total_bytes: number; uploads_bytes: number; warehouse_bytes: number; documents_bytes: number; as_of: string };
    ai: Unavailable | { requests: number; input_tokens: number; output_tokens: number; blocked: number; errors: number; tokens: number; per_day: Array<{ day: string; requests: number; tokens: number }>; covered_from?: string | null; timezone?: string | null };
    pipeline_runs: Unavailable | { total: number; by_status: Record<string, number>; per_day: DayCount[] };
    documents: Unavailable | { total: number; by_status: Record<string, number>; created: number; per_day: DayCount[] };
  }>;
}

export interface Unavailable {
  available: false;
}

/** Narrow a metric block: `null` when absent or reported unavailable. */
export function metricOf<K extends UsageMetric>(report: UsageReport | undefined, key: K): Exclude<NonNullable<UsageReport["metrics"][K]>, Unavailable> | null {
  const m = report?.metrics?.[key];
  if (!m || (typeof m === "object" && "available" in m && m.available === false)) return null;
  return m as Exclude<NonNullable<UsageReport["metrics"][K]>, Unavailable>;
}

export function isUnavailable(report: UsageReport | undefined, key: UsageMetric): boolean {
  const m = report?.metrics?.[key];
  return !!m && typeof m === "object" && "available" in m && m.available === false;
}

/* ---------------- date range ---------------- */

export interface DateRange {
  from: string;
  to: string;
}

const pad = (n: number) => String(n).padStart(2, "0");
export const isoDay = (d: Date) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
const DAY_MS = 86_400_000;
const parseDay = (s: string) => Date.parse(`${s}T00:00:00Z`);

/** The last `days` UTC days ending today (inclusive). */
export function presetRange(days: number, now: Date = new Date()): DateRange {
  const to = isoDay(now);
  return { from: isoDay(new Date(parseDay(to) - (days - 1) * DAY_MS)), to };
}

export function rangeDays(r: DateRange): number {
  return Math.round((parseDay(r.to) - parseDay(r.from)) / DAY_MS) + 1;
}

/** Client-side check mirroring `usage.parse_range` (the server validates too). */
export function validateRange(r: DateRange, now: Date = new Date()): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(r.from) || !/^\d{4}-\d{2}-\d{2}$/.test(r.to) || Number.isNaN(parseDay(r.from)) || Number.isNaN(parseDay(r.to))) {
    return "Choose both dates.";
  }
  if (r.from > r.to) return "The start must not be after the end.";
  if (rangeDays(r) > MAX_USAGE_DAYS) return `At most ${MAX_USAGE_DAYS} days.`;
  if (r.from > isoDay(now)) return "The range starts in the future.";
  return null;
}

/** /ai/usage looks back `days` from today (max 90): enough days to cover `from`. */
export function aiLookbackDays(r: DateRange, now: Date = new Date()): number {
  const back = Math.round((parseDay(isoDay(now)) - parseDay(r.from)) / DAY_MS) + 1;
  return Math.max(1, Math.min(90, back));
}

/* ---------------- series ---------------- */

/** Merge several per-day series into chart rows `{day, a, b}` (missing days are 0). */
export function mergeDaily(series: Record<string, DayCount[] | undefined>): Array<Record<string, number | string>> {
  const rows = new Map<string, Record<string, number | string>>();
  const keys = Object.keys(series);
  for (const [key, points] of Object.entries(series)) {
    for (const p of points ?? []) {
      const row = rows.get(p.day) ?? Object.fromEntries([["day", p.day], ...keys.map((k) => [k, 0])]);
      row[key] = (Number(row[key]) || 0) + (Number(p.count) || 0);
      rows.set(p.day, row);
    }
  }
  return Array.from(rows.values()).sort((a, b) => String(a.day).localeCompare(String(b.day)));
}

/** `{verdict: n}` -> sorted `[{name, value}]` (largest first). */
export function breakdown(map: Record<string, number> | undefined): Array<{ name: string; value: number }> {
  return Object.entries(map ?? {})
    .map(([name, value]) => ({ name, value: Number(value) || 0 }))
    .filter((r) => r.value > 0)
    .sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
}

/** AI usage per model within the range (from /ai/usage `daily`, which is per day x model). */
export function aiByModel(usage: AiUsage | undefined, r: DateRange): Array<{ model: string; requests: number; tokens: number; blocked: number; errors: number }> {
  const out = new Map<string, { model: string; requests: number; tokens: number; blocked: number; errors: number }>();
  for (const d of usage?.daily ?? []) {
    if (d.day < r.from || d.day > r.to) continue;
    const row = out.get(d.model) ?? { model: d.model, requests: 0, tokens: 0, blocked: 0, errors: 0 };
    row.requests += d.requests;
    row.tokens += d.input_tokens + d.output_tokens;
    row.blocked += d.blocked;
    row.errors += d.errors;
    out.set(d.model, row);
  }
  return Array.from(out.values()).sort((a, b) => b.requests - a.requests || a.model.localeCompare(b.model));
}

/** Share of the range's second half vs the first half (a simple trend for KPI cards); null when no baseline. */
export function halfOverHalf(points: DayCount[] | undefined): number | null {
  const p = points ?? [];
  if (p.length < 2) return null;
  const mid = Math.floor(p.length / 2);
  const a = p.slice(0, mid).reduce((s, x) => s + x.count, 0);
  const b = p.slice(p.length - mid).reduce((s, x) => s + x.count, 0);
  if (a === 0) return null;
  return (b - a) / a;
}
