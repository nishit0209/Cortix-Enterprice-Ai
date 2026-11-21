import { formatNumber } from "@/lib/utils";

/** Metric formatting shared by the ML screens (kept apart from the chart module so it doesn't pull in recharts). */
export const fmtMetric = (v: unknown): string => {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1000) return formatNumber(n, { compact: true });
  return n.toFixed(Math.abs(n) < 1 ? 3 : 2);
};
