/** Pure helpers for dashboard KPI deltas (only computed from real data; null when there is nothing to compare). */

import type { AiUsage, DataStats, Dataset } from "./types";

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

/** Previous-period values for KPI deltas: the flat W4 `*_prev` fields win over the older `previous` object. */
export function previousStats(s: DataStats | null | undefined): { datasets: number | null; rows_total: number | null; avg_quality: number | null; pipelines_active: number | null } | null {
  if (!s) return null;
  const pick = (flat: number | null | undefined, legacy: number | null | undefined) => (flat !== undefined && flat !== null ? flat : (legacy ?? null));
  const p = s.previous ?? {};
  const out = {
    datasets: pick(s.datasets_prev, p.datasets),
    rows_total: pick(s.rows_total_prev, p.rows_total),
    avg_quality: pick(s.avg_quality_prev, p.avg_quality),
    pipelines_active: p.pipelines_active ?? null,
  };
  return Object.values(out).some((v) => v !== null) ? out : null;
}
