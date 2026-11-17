"use client";

import * as React from "react";
import { BarChart3, Table2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Card, CardHeader } from "./card";
import { Skeleton } from "./feedback";

export const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
  "var(--chart-7)",
  "var(--chart-8)",
];

/** Shared Recharts props so every chart reads the theme tokens. */
export const chartTheme = {
  grid: { stroke: "var(--chart-grid)", strokeDasharray: "3 3", vertical: false },
  axis: {
    stroke: "var(--chart-axis)",
    tick: { fill: "var(--chart-axis)", fontSize: 11 },
    tickLine: false,
    axisLine: false,
  },
  tooltipCursor: { fill: "var(--surface-2)" },
} as const;

interface TooltipEntry {
  name?: string | number;
  value?: unknown;
  color?: string;
  payload?: Record<string, unknown>;
}

/** Recharts custom tooltip content, styled with tokens. */
export function ChartTooltip({
  active,
  payload,
  label,
  formatter,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: React.ReactNode;
  formatter?: (value: unknown, name: string) => React.ReactNode;
}) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="min-w-36 rounded-control border border-border bg-surface px-3 py-2 text-xs shadow-pop">
      {label !== undefined && label !== "" && <div className="mb-1 font-medium text-fg">{label}</div>}
      <div className="space-y-1">
        {payload.map((p, i) => (
          <div key={i} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 text-fg-muted">
              <span className="size-2 rounded-full" style={{ background: p.color ?? CHART_COLORS[i % 8] }} aria-hidden />
              {String(p.name ?? "")}
            </span>
            <span className="tabular font-medium text-fg">
              {formatter ? formatter(p.value, String(p.name ?? "")) : String(p.value ?? "")}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export interface ChartTableColumn {
  key: string;
  label: string;
  align?: "left" | "right";
  format?: (v: unknown) => React.ReactNode;
}

/**
 * Card wrapper for a chart with loading/empty/error slots and an accessible table toggle
 * (UI/UX spec: charts are paired with table views).
 */
export function ChartCard({
  title,
  description,
  actions,
  loading,
  empty,
  error,
  height = 240,
  table,
  className,
  children,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  loading?: boolean;
  empty?: React.ReactNode;
  error?: React.ReactNode;
  height?: number;
  table?: { columns: ChartTableColumn[]; rows: Array<Record<string, unknown>> };
  className?: string;
  children?: React.ReactNode;
}) {
  const [view, setView] = React.useState<"chart" | "table">("chart");
  const canToggle = !!table && !loading && !error && !empty && table.rows.length > 0;
  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader
        title={title}
        description={description}
        actions={
          <>
            {actions}
            {canToggle && (
              <div className="flex rounded-control border border-border p-0.5" role="group" aria-label="Chart view">
                <ToggleBtn active={view === "chart"} onClick={() => setView("chart")} label="Chart view">
                  <BarChart3 className="size-3.5" />
                </ToggleBtn>
                <ToggleBtn active={view === "table"} onClick={() => setView("table")} label="Table view">
                  <Table2 className="size-3.5" />
                </ToggleBtn>
              </div>
            )}
          </>
        }
      />
      <div className="flex-1 px-5 pb-5" style={{ minHeight: height }}>
        {loading ? (
          <div className="flex h-full items-end gap-2 pt-4" style={{ height }} aria-busy>
            {[0.5, 0.8, 0.35, 0.65, 0.9, 0.45, 0.7].map((h, i) => (
              <Skeleton key={i} className="flex-1 rounded-b-none" style={{ height: `${h * 100}%` }} />
            ))}
          </div>
        ) : error ? (
          error
        ) : empty ? (
          empty
        ) : view === "table" && table ? (
          <div className="scrollbar-thin overflow-auto rounded-control border border-border" style={{ maxHeight: height }}>
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface-2 text-xs text-fg-muted">
                <tr>
                  {table.columns.map((c) => (
                    <th key={c.key} className={cn("px-3 py-2 font-medium", c.align === "right" ? "text-right" : "text-left")}>
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {table.rows.map((r, i) => (
                  <tr key={i} className="border-t border-border">
                    {table.columns.map((c) => (
                      <td key={c.key} className={cn("tabular px-3 py-1.5", c.align === "right" ? "text-right" : "text-left")}>
                        {c.format ? c.format(r[c.key]) : String(r[c.key] ?? "—")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ height }}>{children}</div>
        )}
      </div>
    </Card>
  );
}

function ToggleBtn({
  active,
  onClick,
  label,
  children,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      aria-label={label}
      className={cn(
        "flex size-6 items-center justify-center rounded-[4px] text-fg-subtle transition-colors focus-ring",
        active ? "bg-surface-2 text-fg" : "hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}
