"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import {
  AlertCircle,
  BarChart3,
  Download,
  FileSpreadsheet,
  Info,
  Lightbulb,
  Loader2,
  Pin,
  Sparkles,
  Table2,
  ThumbsDown,
  ThumbsUp,
  TrendingUp,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/overlays";
import { availableChartTypes, normalizeColumns, resolveChart } from "@/lib/chat/chart";
import { followUps } from "@/lib/chat/suggestions";
import { resultColumnNames, type AnswerView } from "@/lib/chat/view";
import { errorMessage } from "@/lib/errors";
import { exportMessage, useMessageFeedback } from "@/lib/queries/bi";
import { formatMs } from "@/hooks/use-countdown";
import type { ChartType, DatasetColumn } from "@/lib/types";
import { cn, formatNumber } from "@/lib/utils";
import { StageStepper } from "./stage-stepper";
import { SqlPanel, SourceBadge } from "./sql-code";
import { ChartTypeSwitcher } from "./chart-meta";
import { ResultChart } from "@/components/lazy-charts";
import { ResultTable } from "./result-table";
import { ChatErrorCard, type ErrorCardContext } from "./error-card";
import { PinDialog } from "@/components/dashboards/dialogs";

const INSIGHT_ICON: Record<string, React.ReactNode> = {
  trend: <TrendingUp className="size-3.5" aria-hidden />,
  anomaly: <AlertCircle className="size-3.5" aria-hidden />,
};

export function UserBubble({ text }: { text: string }) {
  const reduce = useReducedMotion();
  return (
    <motion.div
      initial={reduce ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className="flex justify-end"
    >
      <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-[14px] rounded-br-[4px] bg-accent px-4 py-2.5 text-sm text-accent-fg shadow-soft">
        {text}
      </div>
    </motion.div>
  );
}

export function AnswerCard({
  view,
  question,
  ctx,
  datasetColumns,
  onFollowUp,
  onRetry,
  canExport,
  canPin = false,
}: {
  view: AnswerView;
  question: string;
  ctx: ErrorCardContext;
  datasetColumns: DatasetColumn[];
  onFollowUp: (q: string) => void;
  onRetry?: () => void;
  canExport: boolean;
  /** Show "Pin to dashboard" (dashboards.manage). */
  canPin?: boolean;
}) {
  const [pinOpen, setPinOpen] = React.useState(false);
  const reduce = useReducedMotion();
  const streaming = view.phase === "connecting" || view.phase === "streaming";
  const [override, setOverride] = React.useState<ChartType | null>(null);
  const [tab, setTab] = React.useState<"chart" | "table">("chart");
  const [feedback, setFeedback] = React.useState<"up" | "down" | null>(view.feedback);
  const [exporting, setExporting] = React.useState<"csv" | "xlsx" | null>(null);
  const fb = useMessageFeedback(ctx.slug);

  const result = view.result;
  const columns = React.useMemo(() => (result ? normalizeColumns(result.columns, result.rows) : []), [result]);
  const types = React.useMemo(() => (result ? availableChartTypes(columns, result.rows.length) : []), [columns, result]);
  const spec = React.useMemo(
    () => (result ? resolveChart(view.chart, result.columns, result.rows, override) : null),
    [result, view.chart, override],
  );
  const chartable = !!spec && spec.type !== "table";
  const showTable = !chartable || tab === "table";
  const editorHref =
    view.sql?.executed || view.sql?.raw
      ? `/o/${ctx.slug}/ask/sql?${new URLSearchParams({ ...(ctx.datasetId ? { dataset: ctx.datasetId } : {}), sql: view.sql.raw ?? view.sql.executed ?? "" })}`
      : null;
  const suggestions = React.useMemo(() => {
    if (view.phase !== "done" || !result) return [];
    if (view.suggestions.length) return view.suggestions.slice(0, 3);
    return followUps(question, resultColumnNames(result), datasetColumns);
  }, [view.phase, view.suggestions, result, question, datasetColumns]);

  const sendFeedback = async (correct: boolean) => {
    if (!view.messageId) return;
    const next = correct ? "up" : "down";
    const prev = feedback;
    setFeedback(next);
    try {
      await fb.mutateAsync({ messageId: view.messageId, correct });
      toast.success(correct ? "Thanks — verified answers get reused for similar questions." : "Thanks for the feedback.");
    } catch (e) {
      setFeedback(prev);
      toast.error(errorMessage(e));
    }
  };

  const doExport = async (format: "csv" | "xlsx") => {
    if (!view.messageId) return;
    setExporting(format);
    try {
      const { filename } = await exportMessage(ctx.slug, view.messageId, format);
      toast.success(`Downloaded ${filename}`);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setExporting(null);
    }
  };

  return (
    <motion.article
      initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
      className="flex gap-3"
      aria-busy={streaming}
      aria-label="Cortix answer"
    >
      <span
        className={cn(
          "mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-full brand-gradient text-white shadow-soft",
          streaming && "animate-pulse",
        )}
        aria-hidden
      >
        <Sparkles className="size-4" />
      </span>
      <div className="min-w-0 flex-1 space-y-3">
        {/* status row */}
        {streaming ? (
          <StageStepper stage={view.stage} seen={view.seenStages} active />
        ) : view.phase === "done" && (view.source || view.durationMs) ? (
          <div className="flex flex-wrap items-center gap-2 text-xs text-fg-subtle">
            <SourceBadge source={view.source} />
            {result && (
              <span className="tabular">
                {formatNumber(result.row_count)} row{result.row_count === 1 ? "" : "s"}
              </span>
            )}
            {view.durationMs !== null && <span className="tabular">· {formatMs(view.durationMs)}</span>}
          </div>
        ) : view.phase === "aborted" ? (
          <p className="text-xs text-fg-subtle">Stopped. The question may still finish on the server.</p>
        ) : null}

        {view.error && (
          <ChatErrorCard
            error={view.error}
            sql={view.sql}
            sqlSource={view.sql?.source}
            editorHref={editorHref}
            ctx={ctx}
            onRetry={onRetry}
          />
        )}

        {/* waiting skeleton before anything arrives */}
        {streaming && !result && !view.narration && (
          <div className="flex items-center gap-2 text-sm text-fg-muted">
            <Loader2 className="size-4 animate-spin text-accent" aria-hidden />
            {view.stage === "executing" ? "Running the query…" : view.stage === "generating" ? "Writing SQL…" : "Working on it…"}
          </div>
        )}

        {result && spec && (
          <div className="overflow-hidden rounded-card border border-border bg-surface shadow-xs">
            <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
              <div className="flex rounded-control bg-surface-2 p-0.5 text-xs" role="tablist" aria-label="Result view">
                {chartable && (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={tab === "chart"}
                    onClick={() => setTab("chart")}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-[4px] px-2.5 py-1 font-medium focus-ring",
                      tab === "chart" ? "bg-surface text-fg shadow-xs" : "text-fg-muted hover:text-fg",
                    )}
                  >
                    <BarChart3 className="size-3.5" aria-hidden /> Chart
                  </button>
                )}
                <button
                  type="button"
                  role="tab"
                  aria-selected={showTable}
                  onClick={() => setTab("table")}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-[4px] px-2.5 py-1 font-medium focus-ring",
                    showTable ? "bg-surface text-fg shadow-xs" : "text-fg-muted hover:text-fg",
                  )}
                >
                  <Table2 className="size-3.5" aria-hidden /> Table
                </button>
              </div>
              {result.truncated && (
                <Tooltip content="The result hit the row or byte cap. Add filters or aggregate to see everything.">
                  <span>
                    <Badge tone="warning">truncated</Badge>
                  </span>
                </Tooltip>
              )}
              <span className="tabular text-xs text-fg-subtle">
                {formatNumber(result.row_count)} row{result.row_count === 1 ? "" : "s"}
              </span>
              {chartable && tab === "chart" && types.length > 2 && (
                <div className="ml-auto">
                  <ChartTypeSwitcher
                    types={types.filter((t) => t !== "table")}
                    value={spec.type}
                    onChange={(t) => setOverride(t)}
                  />
                </div>
              )}
            </div>
            <div className={cn(showTable ? "p-3" : "px-3 pb-3 pt-2")}>
              {showTable ? (
                <ResultTable columns={result.columns} rows={result.rows} caption={`Result for: ${question}`} />
              ) : (
                <>
                  <ResultChart spec={spec} />
                  {spec.note && (
                    <p className="mt-1 flex items-center gap-1.5 text-xs text-fg-subtle">
                      <Info className="size-3" aria-hidden /> {spec.note}
                    </p>
                  )}
                </>
              )}
            </div>
          </div>
        )}

        {view.narration && (
          <p className="whitespace-pre-wrap text-sm leading-6 text-fg">
            {view.narration}
            {streaming && <span className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 animate-pulse rounded-sm bg-accent" aria-hidden />}
          </p>
        )}

        {view.insights.length > 0 && (
          <ul className="space-y-1.5" aria-label="Insights">
            {view.insights.map((ins, i) => (
              <motion.li
                key={ins.text}
                initial={reduce ? false : { opacity: 0, x: -4 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.16, delay: Math.min(i, 4) * 0.04 }}
                className="flex gap-2.5 rounded-control bg-accent-soft/50 px-3 py-2 text-sm text-fg"
              >
                <span className="mt-0.5 shrink-0 text-accent">{INSIGHT_ICON[ins.kind] ?? <Lightbulb className="size-3.5" aria-hidden />}</span>
                <span>{ins.text}</span>
              </motion.li>
            ))}
          </ul>
        )}

        {view.assumptions.length > 0 && (
          <div className="rounded-control border border-dashed border-border px-3 py-2">
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-subtle">Assumptions</div>
            <ul className="list-inside list-disc space-y-0.5 text-xs text-fg-muted">
              {view.assumptions.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          </div>
        )}

        {!view.error && (view.sql?.executed || view.sql?.raw) && (
          <SqlPanel
            executed={view.sql.executed}
            raw={view.sql.raw}
            source={view.sql.source}
            editorHref={editorHref}
            meta={view.durationMs !== null ? formatMs(view.durationMs) : undefined}
          />
        )}

        {view.phase === "done" && view.messageId && (
          <div className="flex flex-wrap items-center gap-1">
            <Tooltip content="Correct answer">
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Mark answer as correct"
                aria-pressed={feedback === "up"}
                className={cn(feedback === "up" && "bg-success-soft text-success")}
                onClick={() => void sendFeedback(true)}
              >
                <ThumbsUp />
              </Button>
            </Tooltip>
            <Tooltip content="Wrong answer">
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Mark answer as wrong"
                aria-pressed={feedback === "down"}
                className={cn(feedback === "down" && "bg-danger-soft text-danger")}
                onClick={() => void sendFeedback(false)}
              >
                <ThumbsDown />
              </Button>
            </Tooltip>
            {canPin && result && (view.sql?.executed || view.sql?.raw) && (
              <>
                <span className="mx-1 h-4 w-px bg-border" aria-hidden />
                <Button variant="ghost" size="xs" onClick={() => setPinOpen(true)}>
                  <Pin /> Pin to dashboard
                </Button>
                <PinDialog
                  org={ctx.slug}
                  messageId={view.messageId}
                  defaultTitle={question || "Pinned answer"}
                  chart={override ? { ...(view.chart ?? {}), type: override } : (view.chart ?? (spec ? { type: spec.type, x: spec.x, y: spec.y, series: spec.series } : null))}
                  open={pinOpen}
                  onOpenChange={setPinOpen}
                />
              </>
            )}
            {canExport && result && (
              <>
                <span className="mx-1 h-4 w-px bg-border" aria-hidden />
                <Button variant="ghost" size="xs" loading={exporting === "csv"} onClick={() => void doExport("csv")}>
                  <Download /> CSV
                </Button>
                <Button variant="ghost" size="xs" loading={exporting === "xlsx"} onClick={() => void doExport("xlsx")}>
                  <FileSpreadsheet /> XLSX
                </Button>
              </>
            )}
          </div>
        )}

        {suggestions.length > 0 && (
          <div className="flex flex-wrap gap-1.5" aria-label="Follow-up questions">
            {suggestions.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => onFollowUp(s)}
                className="rounded-full border border-border bg-surface px-3 py-1 text-xs text-fg-muted transition-colors hover:border-accent/40 hover:bg-accent-soft hover:text-accent-soft-fg focus-ring"
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </div>
    </motion.article>
  );
}
