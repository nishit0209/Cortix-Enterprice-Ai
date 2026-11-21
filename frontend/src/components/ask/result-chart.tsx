"use client";

import * as React from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_COLORS, ChartTooltip, chartTheme } from "@/components/ui/chart-card";
import type { ChartSpec } from "@/lib/chat/chart";
import { titleCase } from "@/lib/utils";

import { CHART_TYPE_META, ChartTypeSwitcher, formatValue } from "./chart-meta";

export { CHART_TYPE_META, ChartTypeSwitcher, formatValue };

function shortLabel(v: unknown): string {
  const s = String(v ?? "—");
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) return s.slice(0, 10);
  return s.length > 14 ? `${s.slice(0, 13)}…` : s;
}

export function ResultChart({ spec, height = 280 }: { spec: ChartSpec; height?: number }) {
  const { type, x, keys, data } = spec;
  const multi = keys.length > 1;
  const tooltip = <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => formatValue(v)} />} />;
  const legend = multi ? (
    <Legend
      verticalAlign="top"
      align="right"
      height={28}
      iconType="circle"
      iconSize={8}
      wrapperStyle={{ fontSize: 12, color: "var(--fg-muted)" }}
      formatter={(v: string) => <span style={{ color: "var(--fg-muted)" }}>{titleCase(String(v))}</span>}
    />
  ) : null;

  if (type === "kpi") {
    const metric = keys[0];
    const value = metric ? data[0]?.[metric] : null;
    const others = Object.entries(data[0] ?? {}).filter(([k]) => k !== metric);
    return (
      <div className="flex flex-col items-start justify-center gap-1 py-6" style={{ minHeight: 120 }}>
        <span className="text-xs font-medium uppercase tracking-[0.08em] text-fg-subtle">{metric ? titleCase(metric) : "Value"}</span>
        <span className="tabular text-[40px] font-semibold leading-none tracking-[-0.02em] text-fg">{formatValue(value)}</span>
        {others.length > 0 && (
          <span className="mt-1 text-sm text-fg-muted">
            {others.map(([k, v]) => `${titleCase(k)}: ${formatValue(v)}`).join(" · ")}
          </span>
        )}
      </div>
    );
  }

  const xKey = x ?? "__row";
  const xAxis = (
    <XAxis dataKey={xKey} {...chartTheme.axis} tickFormatter={shortLabel} minTickGap={12} interval="preserveStartEnd" />
  );
  const yAxis = <YAxis {...chartTheme.axis} width={52} tickFormatter={(v: number) => formatValue(v)} />;
  const margin = { top: 8, right: 12, bottom: 4, left: 0 };

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        {type === "line" ? (
          <LineChart data={data} margin={margin}>
            <CartesianGrid {...chartTheme.grid} />
            {xAxis}
            {yAxis}
            {tooltip}
            {legend}
            {keys.map((k, i) => (
              <Line
                key={k}
                type="monotone"
                dataKey={k}
                name={k}
                stroke={CHART_COLORS[i % 8]}
                strokeWidth={2}
                dot={data.length <= 24 ? { r: 3, strokeWidth: 2, fill: "var(--surface)" } : false}
                activeDot={{ r: 5, strokeWidth: 2, stroke: "var(--surface)" }}
                animationDuration={500}
              />
            ))}
          </LineChart>
        ) : type === "area" ? (
          <AreaChart data={data} margin={margin}>
            <defs>
              {keys.map((k, i) => (
                <linearGradient key={k} id={`area-${i}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={CHART_COLORS[i % 8]} stopOpacity={0.28} />
                  <stop offset="100%" stopColor={CHART_COLORS[i % 8]} stopOpacity={0.02} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid {...chartTheme.grid} />
            {xAxis}
            {yAxis}
            {tooltip}
            {legend}
            {keys.map((k, i) => (
              <Area
                key={k}
                type="monotone"
                dataKey={k}
                name={k}
                stackId={multi ? "s" : undefined}
                stroke={CHART_COLORS[i % 8]}
                strokeWidth={2}
                fill={`url(#area-${i})`}
                animationDuration={500}
              />
            ))}
          </AreaChart>
        ) : type === "pie" ? (
          <PieChart>
            <Pie
              data={data}
              dataKey={keys[0]}
              nameKey={xKey}
              innerRadius="52%"
              outerRadius="82%"
              paddingAngle={1}
              stroke="var(--surface)"
              strokeWidth={2}
              animationDuration={500}
            >
              {data.map((_, i) => (
                <Cell key={i} fill={CHART_COLORS[i % 8]} />
              ))}
            </Pie>
            <Tooltip content={<ChartTooltip formatter={(v) => formatValue(v)} />} />
            <Legend
              layout="vertical"
              align="right"
              verticalAlign="middle"
              iconType="circle"
              iconSize={8}
              wrapperStyle={{ fontSize: 12 }}
              formatter={(v: string) => <span style={{ color: "var(--fg-muted)" }}>{shortLabel(v)}</span>}
            />
          </PieChart>
        ) : type === "scatter" ? (
          <ScatterChart margin={margin}>
            <CartesianGrid {...chartTheme.grid} vertical />
            <XAxis dataKey={xKey} type="number" name={xKey} {...chartTheme.axis} tickFormatter={(v: number) => formatValue(v)} />
            <YAxis dataKey={keys[0]} type="number" name={keys[0]} {...chartTheme.axis} width={52} tickFormatter={(v: number) => formatValue(v)} />
            <Tooltip cursor={{ strokeDasharray: "3 3", stroke: "var(--chart-axis)" }} content={<ChartTooltip formatter={(v) => formatValue(v)} />} />
            <Scatter data={data} fill={CHART_COLORS[0]} stroke="var(--surface)" strokeWidth={1.5} animationDuration={500} />
          </ScatterChart>
        ) : (
          <BarChart data={data} margin={margin} barCategoryGap="22%" barGap={2}>
            <CartesianGrid {...chartTheme.grid} />
            {xAxis}
            {yAxis}
            {tooltip}
            {legend}
            {keys.map((k, i) => (
              <Bar
                key={k}
                dataKey={k}
                name={k}
                fill={CHART_COLORS[i % 8]}
                radius={[4, 4, 0, 0]}
                maxBarSize={44}
                animationDuration={500}
              />
            ))}
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
