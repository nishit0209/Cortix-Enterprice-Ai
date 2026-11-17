"use client";

import * as React from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_COLORS, ChartTooltip, chartTheme } from "@/components/ui/chart-card";
import { formatNumber } from "@/lib/utils";

export function RunsDonut({ succeeded, failed }: { succeeded: number; failed: number }) {
  const data = [
    { name: "Succeeded", value: succeeded, color: "var(--success)" },
    { name: "Failed", value: failed, color: "var(--danger)" },
  ];
  const total = succeeded + failed;
  const rate = total > 0 ? Math.round((succeeded / total) * 100) : 0;
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 sm:flex-row sm:gap-8">
      <div className="relative size-[180px] shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={data}
              dataKey="value"
              nameKey="name"
              innerRadius={62}
              outerRadius={84}
              paddingAngle={total > 0 && failed > 0 && succeeded > 0 ? 2 : 0}
              stroke="none"
              isAnimationActive
              animationDuration={600}
            >
              {data.map((d) => (
                <Cell key={d.name} fill={d.color} />
              ))}
            </Pie>
            <Tooltip content={<ChartTooltip formatter={(v) => formatNumber(Number(v))} />} />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="tabular text-2xl font-semibold text-fg">{rate}%</span>
          <span className="text-xs text-fg-muted">success rate</span>
        </div>
      </div>
      <ul className="grid w-full max-w-[200px] gap-3 text-sm">
        {data.map((d) => (
          <li key={d.name} className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-2 text-fg-muted">
              <span className="size-2.5 rounded-full" style={{ background: d.color }} aria-hidden />
              {d.name}
            </span>
            <span className="tabular font-medium text-fg">{formatNumber(d.value)}</span>
          </li>
        ))}
        <li className="flex items-center justify-between gap-3 border-t border-border pt-3">
          <span className="text-fg-muted">Total runs</span>
          <span className="tabular font-semibold text-fg">{formatNumber(total)}</span>
        </li>
      </ul>
    </div>
  );
}

export function RowsByDatasetChart({ data }: { data: Array<{ name: string; rows: number }> }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 4 }} barCategoryGap={6}>
        <CartesianGrid {...chartTheme.grid} horizontal={false} vertical />
        <XAxis
          type="number"
          {...chartTheme.axis}
          tickFormatter={(v: number) => formatNumber(v, { compact: true })}
        />
        <YAxis
          type="category"
          dataKey="name"
          {...chartTheme.axis}
          width={120}
          tickFormatter={(v: string) => (v.length > 16 ? `${v.slice(0, 15)}…` : v)}
        />
        <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => `${formatNumber(Number(v))} rows`} />} />
        <Bar dataKey="rows" name="Rows" radius={[0, 4, 4, 0]} maxBarSize={18} animationDuration={500}>
          {data.map((_, i) => (
            <Cell key={i} fill={i === 0 ? CHART_COLORS[0] : CHART_COLORS[1]} fillOpacity={i === 0 ? 1 : 0.85} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function HistogramChart({ data, color = CHART_COLORS[1] }: { data: Array<{ label: string; count: number }>; color?: string }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }} barCategoryGap={1}>
        <XAxis dataKey="label" hide />
        <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => formatNumber(Number(v))} />} />
        <Bar dataKey="count" name="Count" fill={color} radius={[2, 2, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}
