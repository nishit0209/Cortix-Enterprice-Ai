/** Pure helpers for dashboard KPI deltas (only computed from real data; null when there is nothing to compare). */

import type { AiUsage, Dataset } from "./types";

export function pctChange(current: number | null | undefined, previous: number | null | undefined): number | null {
  if (current === null || current === undefined || previous === null || previous === undefined) return null;
  if (previous === 0) return null;
  return ((current - previous) / Math.abs(previous)) * 100;
}

/** Growth of the dataset count over the last `days`: datasets created in the window vs the count before it. */
export function datasetGrowth(datasets: Pick<Dataset, "created_at" | "status">[], now: number = Date.now(), days = 7): { added: number; pct: number | null } {
  const live = datasets.filter((d) => d.status !== "deleted");
  const cutoff = now - days * 86_400_000;
  const added = live.filter((d) => new Date(d.created_at).getTime() >= cutoff).length;
  const before = live.length - added;
  return { added, pct: before > 0 ? (added / before) * 100 : null };
}

/** Requests per day (summed across models), oldest → newest, filling missing days with 0. */
export function dailyRequests(usage: Pick<AiUsage, "daily" | "range"> | null | undefined): Array<{ day: string; requests: number }> {
  if (!usage) return [];
  const byDay = new Map<string, number>();
  for (const d of usage.daily ?? []) byDay.set(d.day, (byDay.get(d.day) ?? 0) + (d.requests ?? 0));
  const days = [...byDay.keys()].sort();
  const from = usage.range?.from?.slice(0, 10) ?? days[0];
  const to = usage.range?.to?.slice(0, 10) ?? days[days.length - 1];
  if (!from || !to) return [];
  const out: Array<{ day: string; requests: number }> = [];
  for (let t = Date.parse(`${from}T00:00:00Z`); t <= Date.parse(`${to}T00:00:00Z`) && out.length < 120; t += 86_400_000) {
    const day = new Date(t).toISOString().slice(0, 10);
    out.push({ day, requests: byDay.get(day) ?? 0 });
  }
  return out;
}
