"use client";

import * as React from "react";
import { AreaChart as AreaIcon, BarChart3, Gauge, LineChart as LineIcon, PieChart as PieIcon, ScatterChart as ScatterIcon, Table2 } from "lucide-react";
import { toNumber } from "@/lib/chat/chart";
import type { ChartType } from "@/lib/types";
import { cn, formatNumber } from "@/lib/utils";

/** Chart helpers without recharts: type metadata, value formatting and the type switcher. */
export const CHART_TYPE_META: Record<ChartType, { label: string; icon: React.ReactNode }> = {
  bar: { label: "Bar", icon: <BarChart3 /> },
  line: { label: "Line", icon: <LineIcon /> },
  area: { label: "Area", icon: <AreaIcon /> },
  pie: { label: "Pie", icon: <PieIcon /> },
  scatter: { label: "Scatter", icon: <ScatterIcon /> },
  kpi: { label: "KPI", icon: <Gauge /> },
  table: { label: "Table", icon: <Table2 /> },
};

export function formatValue(v: unknown): string {
  const n = toNumber(v);
  if (n === null) return v === null || v === undefined ? "—" : String(v);
  if (Math.abs(n) >= 1000) return formatNumber(n, { compact: Math.abs(n) >= 100_000 });
  return Number.isInteger(n) ? String(n) : n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}


/** Chart type switcher (segmented control). */
export function ChartTypeSwitcher({
  types,
  value,
  onChange,
}: {
  types: ChartType[];
  value: ChartType;
  onChange: (t: ChartType) => void;
}) {
  return (
    <div className="flex rounded-control border border-border p-0.5" role="radiogroup" aria-label="Chart type">
      {types.map((t) => (
        <button
          key={t}
          type="button"
          role="radio"
          aria-checked={value === t}
          aria-label={CHART_TYPE_META[t].label}
          title={CHART_TYPE_META[t].label}
          onClick={() => onChange(t)}
          className={cn(
            "flex size-7 items-center justify-center rounded-[4px] text-fg-subtle transition-colors focus-ring [&_svg]:size-3.5",
            value === t ? "bg-surface-2 text-fg shadow-xs" : "hover:text-fg",
          )}
        >
          {CHART_TYPE_META[t].icon}
        </button>
      ))}
    </div>
  );
}
