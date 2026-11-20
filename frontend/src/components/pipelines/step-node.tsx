"use client";

import * as React from "react";
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import {
  ArrowDownAZ,
  Calculator,
  CaseSensitive,
  Columns3,
  Combine,
  Copy,
  Database,
  DatabaseZap,
  Eraser,
  Filter,
  PaintBucket,
  PenLine,
  Replace,
  Scissors,
  Sigma,
  TriangleAlert,
} from "lucide-react";
import { CATEGORY_META, STEP_BY_TYPE } from "@/lib/pipelines/catalog";
import { cn } from "@/lib/utils";

export interface StepData extends Record<string, unknown> {
  stepType: string;
  config: Record<string, unknown>;
}

export type StepNode = Node<StepData, "step">;

export const STEP_ICONS: Record<string, React.ReactNode> = {
  source: <Database />,
  select_columns: <Columns3 />,
  rename: <PenLine />,
  cast: <Replace />,
  trim_whitespace: <Scissors />,
  change_case: <CaseSensitive />,
  fill_null: <PaintBucket />,
  drop_nulls: <Eraser />,
  deduplicate: <Copy />,
  filter: <Filter />,
  derive: <Calculator />,
  sort: <ArrowDownAZ />,
  aggregate: <Sigma />,
  join: <Combine />,
  destination: <DatabaseZap />,
};

/** Per-canvas context: validation issues and display names, so nodes re-render without rebuilding node objects. */
export const BuilderContext = React.createContext<{
  issues: Record<string, string[]>;
  summary: (id: string, type: string, config: Record<string, unknown>) => string;
  readOnly: boolean;
}>({ issues: {}, summary: () => "", readOnly: false });

const handleCls = "!size-2.5 !border-2 !border-surface !bg-fg-subtle hover:!bg-accent";

export function StepNodeView({ id, data, selected }: NodeProps<StepNode>) {
  const ctx = React.useContext(BuilderContext);
  const spec = STEP_BY_TYPE[data.stepType];
  const issues = ctx.issues[id] ?? [];
  const color = spec ? CATEGORY_META[spec.category].color : "var(--chart-8)";
  const summary = ctx.summary(id, data.stepType, data.config);
  return (
    <div
      className={cn(
        "group relative w-[220px] rounded-card border bg-surface text-left shadow-soft transition-[border-color,box-shadow]",
        selected ? "border-accent ring-3 ring-accent/20" : issues.length ? "border-danger/60" : "border-border hover:border-border-strong",
      )}
    >
      <span className="absolute inset-y-2 left-0 w-[3px] rounded-r-full" style={{ background: color }} aria-hidden />
      {spec?.inputs === 2 ? (
        <>
          <Handle id="left" title="Left input" type="target" position={Position.Left} style={{ top: "32%" }} className={handleCls} isConnectable={!ctx.readOnly} />
          <Handle id="right" title="Right input" type="target" position={Position.Left} style={{ top: "72%" }} className={handleCls} isConnectable={!ctx.readOnly} />
        </>
      ) : spec?.inputs !== 0 ? (
        <Handle type="target" position={Position.Left} className={handleCls} isConnectable={!ctx.readOnly} />
      ) : null}
      <div className="flex items-start gap-2.5 px-3.5 py-2.5">
        <span
          className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-control [&_svg]:size-3.5"
          style={{ background: `color-mix(in oklab, ${color} 14%, transparent)`, color }}
          aria-hidden
        >
          {STEP_ICONS[data.stepType] ?? <Columns3 />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-fg">{spec?.label ?? data.stepType}</div>
          <div className="truncate text-[11px] text-fg-muted">{summary || spec?.description}</div>
        </div>
        {issues.length > 0 && (
          <span className="flex items-center gap-0.5 rounded-full bg-danger-soft px-1.5 py-0.5 text-[10px] font-semibold text-danger" title={issues.join("\n")}>
            <TriangleAlert className="size-3" aria-hidden />
            {issues.length}
            <span className="sr-only"> issues: {issues.join("; ")}</span>
          </span>
        )}
      </div>
      {spec?.outputs !== 0 && <Handle type="source" position={Position.Right} className={handleCls} isConnectable={!ctx.readOnly} />}
    </div>
  );
}

export const nodeTypes = { step: StepNodeView };
