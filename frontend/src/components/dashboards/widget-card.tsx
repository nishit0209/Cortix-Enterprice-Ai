"use client";

import * as React from "react";
import { AlertTriangle, BarChart3, FileQuestion, GripVertical, Lock, MoreHorizontal, Pencil, RefreshCw, Table2, Trash2, Type } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/feedback";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger, Tooltip } from "@/components/ui/overlays";
import { formatValue } from "@/components/ask/chart-meta";
import { ResultChart } from "@/components/lazy-charts";
import { ResultTable } from "@/components/ask/result-table";
import { chartSpecFor, kpiFromData, widgetErrorState } from "@/lib/dashboards/widgets";
import { useWidgetData } from "@/lib/queries/dashboards";
import type { DashboardWidget, WidgetData } from "@/lib/types";
import { cn, formatRelative } from "@/lib/utils";

const KIND_ICON = { chart: BarChart3, table: Table2, kpi: BarChart3, text: Type } as const;

export function WidgetBody({ widget, data, height }: { widget: DashboardWidget; data: WidgetData; height: number }) {
  if (widget.kind === "kpi") {
    const k = kpiFromData(data, widget.chart ?? data.chart);
    return (
      <div className="flex h-full flex-col justify-center gap-1" data-testid="kpi-widget">
        <span className="tabular text-[34px] font-semibold leading-none tracking-[-0.02em] text-fg">{k.value === null ? "—" : formatValue(k.value)}</span>
        <span className="text-xs text-fg-muted">{k.caption ?? k.label}</span>
      </div>
    );
  }
  if (data.rows.length === 0) {
    return <p className="flex h-full items-center justify-center text-sm text-fg-muted">The query returned no rows.</p>;
  }
  if (widget.kind === "table") {
    return <ResultTable columns={data.columns} rows={data.rows} caption={widget.title} pageSize={8} />;
  }
  const spec = chartSpecFor(widget, data);
  return (
    <div className="flex h-full flex-col">
      {spec.type === "table" ? (
        <ResultTable columns={data.columns} rows={data.rows} caption={widget.title} pageSize={8} />
      ) : (
        <ResultChart spec={spec} height={Math.max(120, height)} />
      )}
      {spec.note && <p className="mt-1 text-[11px] text-fg-subtle">{spec.note}</p>}
    </div>
  );
}

/**
 * One dashboard tile. Data widgets fetch `/widgets/{id}/data` (re-run under the viewer's policy) with refresh,
 * loading, error and permission states; text widgets render their text.
 */
export function WidgetCard({
  org,
  dashboardId,
  widget,
  editing,
  canEdit,
  bodyHeight,
  onEdit,
  onRemove,
  dragHandleProps,
  className,
}: {
  org: string;
  dashboardId: string;
  widget: DashboardWidget;
  editing?: boolean;
  canEdit?: boolean;
  bodyHeight: number;
  onEdit?: () => void;
  onRemove?: () => void;
  dragHandleProps?: React.HTMLAttributes<HTMLDivElement>;
  className?: string;
}) {
  const q = useWidgetData(org, dashboardId, widget);
  const Icon = KIND_ICON[widget.kind] ?? BarChart3;
  const err = q.isError ? widgetErrorState(q.error) : null;
  return (
    <section
      aria-label={widget.title}
      className={cn("flex h-full min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface shadow-xs", editing && "ring-1 ring-accent/30", className)}
    >
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        {editing ? (
          <div {...dragHandleProps} className={cn("flex min-w-0 flex-1 cursor-grab items-center gap-1.5 active:cursor-grabbing", dragHandleProps?.className)}>
            <GripVertical className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
            <h3 className="truncate text-sm font-medium text-fg">{widget.title}</h3>
          </div>
        ) : (
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            <Icon className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
            <h3 className="truncate text-sm font-medium text-fg">{widget.title}</h3>
          </div>
        )}
        {widget.kind !== "text" && q.data?.refreshed_at && !editing && (
          <span className="hidden text-[11px] text-fg-subtle sm:inline" title={q.data.refreshed_at}>
            {formatRelative(q.data.refreshed_at)}
          </span>
        )}
        {widget.kind !== "text" && (
          <Tooltip content="Refresh (re-runs the query)">
            <Button variant="ghost" size="icon-xs" aria-label={`Refresh ${widget.title}`} onClick={() => void q.refetch()} disabled={q.isFetching}>
              <RefreshCw className={cn(q.isFetching && "animate-spin")} />
            </Button>
          </Tooltip>
        )}
        {canEdit && (onEdit || onRemove) && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon-xs" aria-label={`Widget actions for ${widget.title}`}>
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {onEdit && (
                <DropdownMenuItem onSelect={onEdit}>
                  <Pencil /> Edit
                </DropdownMenuItem>
              )}
              {onEdit && onRemove && <DropdownMenuSeparator />}
              {onRemove && (
                <DropdownMenuItem onSelect={onRemove} className="text-danger">
                  <Trash2 /> Remove
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </header>
      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto p-3">
        {widget.kind === "text" ? (
          <p className="whitespace-pre-wrap text-sm leading-6 text-fg">{widget.text || <span className="text-fg-subtle">Empty note.</span>}</p>
        ) : q.isLoading ? (
          <div className="flex h-full flex-col gap-2" role="status" aria-label={`Loading ${widget.title}`}>
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="w-full flex-1" />
          </div>
        ) : err ? (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 px-2 text-center" role={err.kind === "permission" ? "note" : "alert"}>
            {err.kind === "permission" ? (
              <Lock className="size-4 text-fg-subtle" aria-hidden />
            ) : err.kind === "missing" ? (
              <FileQuestion className="size-4 text-fg-subtle" aria-hidden />
            ) : (
              <AlertTriangle className="size-4 text-danger" aria-hidden />
            )}
            <span className="text-sm font-medium text-fg">{err.title}</span>
            <span className="text-xs text-fg-muted">{err.description}</span>
            {err.kind !== "permission" && err.kind !== "missing" && (
              <Button variant="link" size="xs" onClick={() => void q.refetch()}>
                Try again
              </Button>
            )}
          </div>
        ) : q.data ? (
          <WidgetBody widget={widget} data={q.data} height={bodyHeight} />
        ) : (
          // No data, no error and no fetch running (e.g. the request was cancelled): say so and offer to load it.
          <div className="flex h-full flex-col items-center justify-center gap-1.5 px-2 text-center" role="note" aria-label={`${widget.title} not loaded`}>
            <span className="text-sm text-fg-muted">This widget&apos;s data isn&apos;t loaded.</span>
            <Button variant="link" size="xs" onClick={() => void q.refetch()}>
              Load data
            </Button>
          </div>
        )}
      </div>
      {q.data?.truncated && widget.kind !== "kpi" && (
        <div className="border-t border-border px-3 py-1 text-[11px] text-fg-subtle">Showing the first {q.data.rows.length} rows</div>
      )}
    </section>
  );
}
