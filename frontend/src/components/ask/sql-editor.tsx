"use client";

import * as React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, Columns3, EyeOff, Play, ShieldCheck, SquareTerminal, Timer } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState, Kbd, Skeleton } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { FeatureStatusBadge } from "@/components/ui/status";
import { Tooltip } from "@/components/ui/overlays";
import { isApiError } from "@/lib/api";
import { useManualQuery } from "@/lib/queries/bi";
import { useDatasetColumns, useDatasets } from "@/lib/queries/datasets";
import { SQL_TOKEN_CLASS, tokenizeSql } from "@/lib/sql-highlight";
import { formatMs } from "@/hooks/use-countdown";
import type { ManualQueryResult } from "@/lib/types";
import { cn, formatNumber } from "@/lib/utils";
import { DatasetPicker, isReadyDataset } from "./dataset-picker";
import { ChatErrorCard } from "./error-card";
import { ResultTable } from "./result-table";
import { SqlPanel } from "./sql-code";

/**
 * Code editor = transparent <textarea> over a highlighted <pre> (same font metrics, synced scroll).
 * Keeps native editing, undo, IME and accessibility; Tab inserts two spaces; Ctrl/Cmd+Enter runs.
 */
export function SqlTextEditor({
  value,
  onChange,
  onRun,
  id = "sql-editor",
  minHeight = 220,
}: {
  value: string;
  onChange: (v: string) => void;
  onRun?: () => void;
  id?: string;
  minHeight?: number;
}) {
  const preRef = React.useRef<HTMLPreElement>(null);
  const tokens = React.useMemo(() => tokenizeSql(value), [value]);
  const shared = "m-0 whitespace-pre-wrap break-words px-4 py-3 font-mono text-[13px] leading-6 tracking-normal";
  return (
    <div className="relative overflow-hidden rounded-card border border-border bg-surface-2/60 focus-within:border-accent focus-within:ring-3 focus-within:ring-accent/15">
      <pre ref={preRef} aria-hidden className={cn(shared, "pointer-events-none absolute inset-0 overflow-hidden text-fg")}>
        {tokens.map((t, i) => (t.type === "space" ? t.text : <span key={i} className={SQL_TOKEN_CLASS[t.type]}>{t.text}</span>))}
        {"\n"}
      </pre>
      <label htmlFor={id} className="sr-only">
        SQL query
      </label>
      <textarea
        id={id}
        value={value}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        onChange={(e) => onChange(e.target.value)}
        onScroll={(e) => {
          if (preRef.current) preRef.current.scrollTop = e.currentTarget.scrollTop;
        }}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            onRun?.();
          } else if (e.key === "Tab" && !e.shiftKey) {
            e.preventDefault();
            const ta = e.currentTarget;
            const { selectionStart: s, selectionEnd: end } = ta;
            const next = `${value.slice(0, s)}  ${value.slice(end)}`;
            onChange(next);
            requestAnimationFrame(() => ta.setSelectionRange(s + 2, s + 2));
          }
        }}
        style={{ minHeight }}
        className={cn(shared, "relative block w-full resize-y bg-transparent text-transparent caret-[var(--fg)] outline-none selection:bg-accent/25")}
      />
    </div>
  );
}

function defaultSql(table: string) {
  return `SELECT *\nFROM ${table}\nLIMIT 100`;
}

export function SqlEditorView() {
  const { slug, can } = useOrgContext();
  const params = useSearchParams();
  const datasets = useDatasets(slug);
  const [datasetId, setDatasetId] = React.useState<string | null>(params.get("dataset"));
  const [sql, setSql] = React.useState<string>(params.get("sql") ?? "");
  const run = useManualQuery(slug);
  const [result, setResult] = React.useState<ManualQueryResult | null>(null);
  const [lastSql, setLastSql] = React.useState<string>("");
  const ready = React.useMemo(() => (datasets.data ?? []).filter(isReadyDataset), [datasets.data]);
  const dataset = ready.find((d) => d.id === datasetId);
  const columns = useDatasetColumns(slug, datasetId ?? "", !!dataset);

  React.useEffect(() => {
    if (!datasetId && ready[0]) setDatasetId(ready[0].id);
  }, [datasetId, ready]);
  React.useEffect(() => {
    if (dataset && !sql) setSql(defaultSql(dataset.slug));
  }, [dataset, sql]);

  const execute = async () => {
    if (!datasetId || !sql.trim() || run.isPending) return;
    setLastSql(sql);
    setResult(null);
    try {
      setResult(await run.mutateAsync({ dataset_id: datasetId, sql }));
    } catch {
      /* rendered from run.error */
    }
  };

  if (!can("bi.sql")) {
    return (
      <Card>
        <EmptyState
          icon={<SquareTerminal />}
          title="SQL editor not available for your role"
          description="Running manual SQL needs the bi.sql permission. Ask an owner or admin, or use Ask AI."
          action={
            <Button variant="secondary" asChild>
              <Link href={`/o/${slug}/ask`}>Back to Ask AI</Link>
            </Button>
          }
        />
      </Card>
    );
  }

  const err = run.error;
  const errBody = isApiError(err) ? ((err.body ?? {}) as { reject_code?: string; executed_sql?: string }) : {};

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/ask`} className="inline-flex items-center gap-1 normal-case tracking-normal hover:text-fg">
            <ArrowLeft className="size-3" /> Ask AI
          </Link>
        }
        title="SQL editor"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Run read-only SQL through the same guard as AI answers: your column, mask and row policies are applied, and every query is audited."
      />
      <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
        <Card className="h-fit lg:sticky lg:top-20">
          <CardHeader icon={<Columns3 />} title="Schema" description="Click a column to insert it" />
          <CardContent className="space-y-3">
            <DatasetPicker datasets={datasets.data ?? []} value={datasetId} onChange={(id) => { setDatasetId(id); setSql(""); setResult(null); run.reset(); }} />
            {dataset && (
              <Tooltip content="Logical table name to use in FROM">
                <button
                  type="button"
                  onClick={() => setSql((s) => `${s}${s.endsWith(" ") || !s ? "" : " "}${dataset.slug}`)}
                  className="w-full truncate rounded-control bg-surface-2 px-2.5 py-1.5 text-left font-mono text-xs text-accent hover:bg-surface-3 focus-ring"
                >
                  {dataset.slug}
                </button>
              </Tooltip>
            )}
            <ul className="scrollbar-thin max-h-[360px] space-y-0.5 overflow-y-auto" aria-label="Columns">
              {columns.isLoading
                ? [0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-6" />)
                : (columns.data ?? []).map((c) => (
                    <li key={c.name}>
                      <button
                        type="button"
                        onClick={() => setSql((s) => `${s}${s.endsWith(" ") || s.endsWith("\n") || !s ? "" : " "}${c.name}`)}
                        className="flex w-full items-center justify-between gap-2 rounded-[4px] px-2 py-1 text-left text-xs hover:bg-surface-2 focus-ring"
                      >
                        <span className="flex min-w-0 items-center gap-1.5 truncate font-mono text-fg">
                          {c.is_pii && <EyeOff className="size-3 shrink-0 text-warning" aria-label="PII" />}
                          {c.name}
                        </span>
                        <span className="shrink-0 text-fg-subtle">{c.data_type}</span>
                      </button>
                    </li>
                  ))}
            </ul>
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4">
          <SqlTextEditor value={sql} onChange={setSql} onRun={() => void execute()} />
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={() => void execute()} loading={run.isPending} disabled={!datasetId || !sql.trim()}>
              <Play /> Run query
            </Button>
            <span className="flex items-center gap-1 text-xs text-fg-subtle">
              <Kbd>Ctrl</Kbd>+<Kbd>Enter</Kbd>
            </span>
            <span className="ml-auto flex items-center gap-1.5 text-xs text-fg-muted">
              <ShieldCheck className="size-3.5 text-teal" aria-hidden /> SELECT only · policies enforced · audited
            </span>
          </div>

          {err ? (
            <ChatErrorCard
              error={{
                code: isApiError(err) ? err.code : "error",
                detail: isApiError(err) ? err.detail : String(err),
                rejectCode: errBody.reject_code ?? null,
                requestId: isApiError(err) ? err.requestId : null,
              }}
              sql={{ raw: lastSql, executed: errBody.executed_sql ?? null }}
              sqlSource="manual"
              ctx={{ slug }}
            />
          ) : run.isPending ? (
            <Skeleton className="h-64 rounded-card" />
          ) : result ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                <Badge tone="success" dot>
                  Allowed
                </Badge>
                <span className="tabular">{formatNumber(result.row_count)} rows</span>
                {result.duration_ms !== undefined && (
                  <span className="tabular flex items-center gap-1">
                    <Timer className="size-3" aria-hidden /> {formatMs(result.duration_ms)}
                  </span>
                )}
                {result.truncated && <Badge tone="warning">truncated at the row cap</Badge>}
              </div>
              <SqlPanel executed={result.executed_sql} raw={lastSql} source="manual" />
              <ResultTable columns={result.columns} rows={result.rows} caption="Query result" pageSize={25} />
            </div>
          ) : (
            <Card>
              <EmptyState
                compact
                icon={<SquareTerminal />}
                title="Run a query to see results"
                description="Only one SELECT statement is allowed. The guard adds limits and your row filters automatically."
              />
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
