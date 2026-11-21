"use client";

/**
 * ML Workspace charts (Recharts + token-driven SVG/CSS heatmaps). Every chart renders only what the API
 * returned; empty inputs render nothing (callers show an empty state). Colors come from the theme tokens so
 * light/dark both work: series use the fixed categorical order (`--chart-1..8`), magnitude heatmaps one hue
 * (light -> dark), correlation a diverging pair with a neutral midpoint, drift the reserved status colors (always
 * paired with a text label).
 */

import * as React from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_COLORS, ChartTooltip, chartTheme } from "@/components/ui/chart-card";
import { cn, formatNumber } from "@/lib/utils";
import type { ConfusionMatrix, CurvePoint, FeatureDrift, Importance, ResidualPoint } from "@/lib/ml/types";
import { PSI_MODERATE, PSI_STABLE, PSI_TONE, confusionCells } from "@/lib/ml/mappers";

import { fmtMetric } from "./format";

export { fmtMetric };

const tick = (v: unknown) => fmtMetric(v);

/* ---------------- heatmaps ---------------- */

/** Sequential single-hue fill for a 0..1 share (surface -> --chart-1). */
export function seqFill(share: number): string {
  const pct = Math.round(Math.max(0, Math.min(1, share)) * 88) + 4;
  return `color-mix(in oklab, var(--chart-1) ${pct}%, var(--surface))`;
}

/** Diverging fill for -1..1 (orange poles <- neutral -> indigo). */
export function divFill(v: number | null): string {
  if (v === null) return "var(--surface-2)";
  const pct = Math.round(Math.min(1, Math.abs(v)) * 85);
  return `color-mix(in oklab, ${v >= 0 ? "var(--chart-1)" : "var(--chart-3)"} ${pct}%, var(--surface-2))`;
}

const inkFor = (share: number) => (share > 0.55 ? "text-white" : "text-fg");

export function CorrelationHeatmap({ columns, matrix, max = 18 }: { columns: string[]; matrix: Array<Array<number | null>>; max?: number }) {
  const n = Math.min(columns.length, max);
  const cols = columns.slice(0, n);
  return (
    <div className="space-y-3">
      <div className="scrollbar-thin overflow-x-auto">
        <div
          role="table"
          aria-label="Correlation matrix"
          className="inline-grid gap-[2px] text-[11px]"
          style={{ gridTemplateColumns: `minmax(90px,140px) repeat(${n}, minmax(30px, 1fr))` }}
        >
          <div role="row" className="contents">
            <span role="columnheader" />
            {cols.map((c) => (
              <span key={c} role="columnheader" className="flex h-24 items-end justify-center pb-1" title={c}>
                <span className="block max-h-24 truncate text-fg-muted [writing-mode:vertical-rl] rotate-180">{c}</span>
              </span>
            ))}
          </div>
          {cols.map((row, i) => (
            <div role="row" key={row} className="contents">
              <span role="rowheader" className="truncate pr-2 text-right leading-[30px] text-fg-muted" title={row}>
                {row}
              </span>
              {cols.map((col, j) => {
                const v = matrix[i]?.[j] ?? null;
                return (
                  <span
                    role="cell"
                    key={col}
                    title={`${row} × ${col}: ${v === null ? "n/a" : v.toFixed(2)}`}
                    aria-label={`${row} and ${col}: ${v === null ? "not available" : v.toFixed(2)}`}
                    className={cn("flex h-[30px] items-center justify-center rounded-[3px] tabular transition-transform hover:scale-110 hover:ring-2 hover:ring-surface", inkFor(Math.abs(v ?? 0)))}
                    style={{ background: divFill(v) }}
                  >
                    {n <= 10 && v !== null ? v.toFixed(1) : ""}
                  </span>
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="flex items-center gap-2 text-[11px] text-fg-muted" aria-hidden>
        <span>−1</span>
        <span className="h-2 w-40 rounded-full" style={{ background: `linear-gradient(90deg, ${divFill(-1)}, ${divFill(0)}, ${divFill(1)})` }} />
        <span>+1</span>
        {columns.length > n && <span className="ml-2">Showing the first {n} of {columns.length} columns.</span>}
      </div>
    </div>
  );
}

export function ConfusionHeatmap({ cm }: { cm: ConfusionMatrix }) {
  const cells = confusionCells(cm);
  const n = cm.labels.length;
  return (
    <div className="space-y-2">
      <div className="scrollbar-thin overflow-x-auto">
        <div role="table" aria-label="Confusion matrix" className="inline-grid gap-[2px] text-xs" style={{ gridTemplateColumns: `minmax(64px,110px) repeat(${n}, minmax(52px, 1fr))` }}>
          <div role="row" className="contents">
            <span role="columnheader" className="text-[10px] uppercase tracking-wide text-fg-subtle">Actual ╲ Pred.</span>
            {cm.labels.map((l) => (
              <span key={l} role="columnheader" className="truncate pb-1 text-center font-medium text-fg-muted" title={l}>
                {l}
              </span>
            ))}
          </div>
          {cm.labels.map((actual, i) => (
            <div role="row" key={actual} className="contents">
              <span role="rowheader" className="truncate pr-2 text-right leading-[44px] font-medium text-fg-muted" title={actual}>
                {actual}
              </span>
              {cells.slice(i * n, i * n + n).map((c) => (
                <span
                  role="cell"
                  key={c.predicted}
                  title={`Actual ${c.actual}, predicted ${c.predicted}: ${formatNumber(c.count)} (${Math.round(c.share * 100)}% of ${c.actual})`}
                  className={cn("flex h-11 flex-col items-center justify-center rounded-[4px] tabular", inkFor(c.share), c.actual === c.predicted && "ring-1 ring-inset ring-fg/20")}
                  style={{ background: seqFill(c.share) }}
                >
                  <span className="font-semibold">{formatNumber(c.count)}</span>
                  <span className="text-[10px] opacity-80">{Math.round(c.share * 100)}%</span>
                </span>
              ))}
            </div>
          ))}
        </div>
      </div>
      <p className="text-[11px] text-fg-subtle">Shading is the share of each actual class (row-normalized recall).</p>
    </div>
  );
}

/* ---------------- curves / scatter ---------------- */

export interface CurveSeries {
  id: string;
  label: string;
  color: string;
  points: CurvePoint[];
}

export function CurveChart({ series, kind, height = 260 }: { series: CurveSeries[]; kind: "roc" | "pr"; height?: number }) {
  const [xl, yl] = kind === "roc" ? ["False positive rate", "True positive rate"] : ["Recall", "Precision"];
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart margin={{ top: 8, right: 12, bottom: 18, left: 0 }}>
        <CartesianGrid {...chartTheme.grid} vertical />
        <XAxis type="number" dataKey="x" domain={[0, 1]} {...chartTheme.axis} tickFormatter={(v) => Number(v).toFixed(1)} label={{ value: xl, position: "insideBottom", offset: -10, fill: "var(--chart-axis)", fontSize: 11 }} />
        <YAxis type="number" dataKey="y" domain={[0, 1]} {...chartTheme.axis} width={44} tickFormatter={(v) => Number(v).toFixed(1)} label={{ value: yl, angle: -90, position: "insideLeft", offset: 10, fill: "var(--chart-axis)", fontSize: 11 }} />
        {kind === "roc" && <ReferenceLine segment={[{ x: 0, y: 0 }, { x: 1, y: 1 }]} stroke="var(--chart-axis)" strokeDasharray="4 4" ifOverflow="extendDomain" />}
        <Tooltip content={<ChartTooltip formatter={(v) => fmtMetric(v)} />} labelFormatter={(v) => `${xl} ${Number(v).toFixed(2)}`} />
        {series.length > 1 && <Legend iconType="plainline" wrapperStyle={{ fontSize: 11, color: "var(--fg-muted)" }} />}
        {series.map((s) => (
          <Line key={s.id} data={s.points} dataKey="y" name={s.label} type="linear" stroke={s.color} strokeWidth={2} dot={false} isAnimationActive animationDuration={500} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

export function ResidualScatter({ points, color = CHART_COLORS[0], height = 260 }: { points: ResidualPoint[]; color?: string; height?: number }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ScatterChart margin={{ top: 8, right: 12, bottom: 18, left: 0 }}>
        <CartesianGrid {...chartTheme.grid} vertical />
        <XAxis type="number" dataKey="predicted" name="Predicted" {...chartTheme.axis} tickFormatter={tick} label={{ value: "Predicted", position: "insideBottom", offset: -10, fill: "var(--chart-axis)", fontSize: 11 }} />
        <YAxis type="number" dataKey="residual" name="Residual" {...chartTheme.axis} width={48} tickFormatter={tick} />
        <ReferenceLine y={0} stroke="var(--chart-axis)" strokeDasharray="4 4" />
        <Tooltip cursor={{ strokeDasharray: "3 3" }} content={<ChartTooltip formatter={(v) => fmtMetric(v)} />} />
        <Scatter data={points} fill={color} fillOpacity={0.55} isAnimationActive={false} shape="circle" />
      </ScatterChart>
    </ResponsiveContainer>
  );
}

/* ---------------- bars ---------------- */

/** Horizontal bars (feature importance, SHAP, missingness). */
export function HBarChart({
  items,
  color = CHART_COLORS[0],
  valueLabel = "Value",
  format = fmtMetric,
  height,
  colorFor,
}: {
  items: Importance[];
  color?: string;
  valueLabel?: string;
  format?: (v: number) => string;
  height?: number;
  colorFor?: (item: Importance) => string;
}) {
  const h = height ?? Math.max(120, items.length * 26 + 30);
  return (
    <ResponsiveContainer width="100%" height={h}>
      <BarChart data={items} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 4 }} barCategoryGap={4}>
        <CartesianGrid {...chartTheme.grid} horizontal={false} vertical />
        <XAxis type="number" {...chartTheme.axis} tickFormatter={(v) => format(Number(v))} />
        <YAxis type="category" dataKey="feature" {...chartTheme.axis} width={130} tick={{ fill: "var(--fg-muted)", fontSize: 11 }} tickFormatter={(v: string) => (v.length > 20 ? `${v.slice(0, 19)}…` : v)} />
        <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => format(Number(v))} />} />
        <Bar dataKey="value" name={valueLabel} radius={[0, 4, 4, 0]} maxBarSize={18} isAnimationActive animationDuration={500}>
          {items.map((it) => (
            <Cell key={it.feature} fill={colorFor ? colorFor(it) : color} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Vertical count bars (class balance, distributions, time coverage). */
export function CountBars({ data, color = CHART_COLORS[0], height = 220, name = "Rows" }: { data: Array<{ label: string; count: number }>; color?: string; height?: number; name?: string }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
        <CartesianGrid {...chartTheme.grid} />
        <XAxis dataKey="label" {...chartTheme.axis} interval="preserveStartEnd" tickFormatter={(v: string) => (v.length > 12 ? `${v.slice(0, 11)}…` : v)} />
        <YAxis {...chartTheme.axis} width={44} tickFormatter={(v) => formatNumber(Number(v), { compact: true })} />
        <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => formatNumber(Number(v))} />} />
        <Bar dataKey="count" name={name} fill={color} radius={[4, 4, 0, 0]} maxBarSize={44} isAnimationActive animationDuration={500} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Grouped metric bars: one group per metric, one bar per experiment (legend + fixed color per experiment). */
export function MetricBarsChart({
  rows,
  series,
  height = 260,
}: {
  rows: Array<Record<string, string | number | null>>;
  series: Array<{ id: string; label: string; color: string }>;
  height?: number;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 4, left: 0 }} barGap={2}>
        <CartesianGrid {...chartTheme.grid} />
        <XAxis dataKey="metric" {...chartTheme.axis} />
        <YAxis {...chartTheme.axis} width={44} tickFormatter={tick} />
        <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => fmtMetric(v)} />} />
        <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11, color: "var(--fg-muted)" }} />
        {series.map((s) => (
          <Bar key={s.id} dataKey={s.id} name={s.label} fill={s.color} radius={[4, 4, 0, 0]} maxBarSize={28} isAnimationActive animationDuration={500} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

/** PSI per feature, colored by the reserved status thresholds (with reference lines at 0.1 and 0.25). */
export function PsiBars({ features }: { features: FeatureDrift[] }) {
  const items = features.slice(0, 30).map((f) => ({ feature: f.feature, value: f.psi, level: f.level }));
  const h = Math.max(140, items.length * 26 + 40);
  return (
    <ResponsiveContainer width="100%" height={h}>
      <BarChart data={items} layout="vertical" margin={{ top: 4, right: 20, bottom: 4, left: 4 }} barCategoryGap={4}>
        <CartesianGrid {...chartTheme.grid} horizontal={false} vertical />
        <XAxis type="number" {...chartTheme.axis} tickFormatter={(v) => Number(v).toFixed(2)} domain={[0, (max: number) => Math.max(0.3, max)]} />
        <YAxis type="category" dataKey="feature" {...chartTheme.axis} width={130} tick={{ fill: "var(--fg-muted)", fontSize: 11 }} />
        <ReferenceLine x={PSI_STABLE} stroke="var(--warning)" strokeDasharray="4 4" />
        <ReferenceLine x={PSI_MODERATE} stroke="var(--danger)" strokeDasharray="4 4" />
        <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => Number(v).toFixed(3)} />} />
        <Bar dataKey="value" name="PSI" radius={[0, 4, 4, 0]} maxBarSize={18} isAnimationActive animationDuration={500}>
          {items.map((it) => (
            <Cell key={it.feature} fill={PSI_TONE[it.level].color} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Reference (training) vs current prediction distribution. */
export function DistributionCompare({ data, height = 220 }: { data: Array<{ bin: string; reference: number | null; current: number | null }>; height?: number }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 0 }} barGap={2}>
        <CartesianGrid {...chartTheme.grid} />
        <XAxis dataKey="bin" {...chartTheme.axis} />
        <YAxis {...chartTheme.axis} width={44} tickFormatter={tick} />
        <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => fmtMetric(v)} />} />
        <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11, color: "var(--fg-muted)" }} />
        <Bar dataKey="reference" name="Training" fill={CHART_COLORS[0]} radius={[4, 4, 0, 0]} maxBarSize={28} />
        <Bar dataKey="current" name="Recent" fill={CHART_COLORS[1]} radius={[4, 4, 0, 0]} maxBarSize={28} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Diverging contribution bars for one prediction (positive pushes up, negative pushes down). */
export function ContributionBars({ items }: { items: Importance[] }) {
  const max = Math.max(...items.map((i) => Math.abs(i.value)), 1e-9);
  return (
    <ul className="space-y-1.5" aria-label="Top contributions">
      {items.map((it) => {
        const w = (Math.abs(it.value) / max) * 50;
        const pos = it.value >= 0;
        return (
          <li key={it.feature} className="grid grid-cols-[minmax(0,140px)_1fr_64px] items-center gap-3 text-xs">
            <span className="truncate text-fg-muted" title={it.feature}>
              {it.feature}
            </span>
            <span className="relative h-3 rounded-full bg-surface-2">
              <span className="absolute inset-y-0 left-1/2 w-px bg-border-strong" aria-hidden />
              <span
                className="absolute inset-y-0 rounded-full transition-[width] duration-500"
                style={{ background: pos ? "var(--chart-1)" : "var(--chart-3)", width: `${w}%`, left: pos ? "50%" : `${50 - w}%` }}
              />
            </span>
            <span className="tabular text-right font-medium text-fg">
              {pos ? "+" : "−"}
              {fmtMetric(Math.abs(it.value))}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
