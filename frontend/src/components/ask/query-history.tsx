"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowLeft, History, SquareTerminal } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { FeatureStatusBadge } from "@/components/ui/status";
import { Tooltip } from "@/components/ui/overlays";
import { guardMessage, SOURCE_LABELS, verdictLabel, verdictTone } from "@/lib/chat/error-copy";
import { isUnavailableEndpoint } from "@/lib/errors";
import { historyDatasetId, historyDatasetName, useQueryHistory } from "@/lib/queries/bi";
import { formatMs } from "@/hooks/use-countdown";
import type { QueryHistoryItem } from "@/lib/types";
import { formatDateTime, formatNumber, formatRelative } from "@/lib/utils";
import { SourceBadge, SqlCode } from "./sql-code";

export function VerdictBadge({ verdict }: { verdict: string }) {
  return (
    <Badge tone={verdictTone(verdict)} dot>
      {verdictLabel(verdict)}
    </Badge>
  );
}

export function QueryHistoryView() {
  const { slug, can } = useOrgContext();
  const q = useQueryHistory(slug);
  const [open, setOpen] = React.useState<QueryHistoryItem | null>(null);

  const columns = React.useMemo<DataTableColumn<QueryHistoryItem>[]>(
    () => [
      {
        id: "question",
        accessorFn: (r) => r.question ?? r.executed_sql ?? "",
        header: "Question / SQL",
        cell: ({ row }) => (
          <span className="block max-w-[420px] truncate" title={row.original.question ?? row.original.executed_sql ?? ""}>
            {row.original.question ?? <span className="font-mono text-xs text-fg-muted">{row.original.executed_sql}</span>}
          </span>
        ),
      },
      {
        id: "dataset",
        accessorFn: (r) => historyDatasetName(r),
        header: "Dataset",
        meta: { hideOnMobile: true },
      },
      {
        id: "source",
        accessorKey: "source",
        header: "Source",
        cell: ({ row }) => <SourceBadge source={row.original.source} />,
      },
      {
        id: "verdict",
        accessorKey: "verdict",
        header: "Verdict",
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1.5">
            <VerdictBadge verdict={row.original.verdict} />
            {row.original.reject_code && (
              <Tooltip content={guardMessage(row.original.reject_code) ?? row.original.reject_code}>
                <span className="font-mono text-[11px] text-fg-subtle">{row.original.reject_code}</span>
              </Tooltip>
            )}
          </span>
        ),
      },
      {
        id: "rows",
        accessorFn: (r) => r.row_count ?? -1,
        header: "Rows",
        meta: { align: "right" },
        cell: ({ row }) => formatNumber(row.original.row_count),
      },
      {
        id: "duration",
        accessorFn: (r) => r.duration_ms ?? -1,
        header: "Duration",
        meta: { align: "right" },
        cell: ({ row }) => <span className="tabular">{formatMs(row.original.duration_ms)}</span>,
      },
      {
        id: "user",
        accessorFn: (r) => r.user_email ?? "",
        header: "User",
        meta: { hideOnMobile: true },
      },
      {
        id: "occurred_at",
        accessorKey: "occurred_at",
        header: "When",
        cell: ({ row }) => (
          <Tooltip content={formatDateTime(row.original.occurred_at)}>
            <span className="whitespace-nowrap text-fg-muted">{formatRelative(row.original.occurred_at)}</span>
          </Tooltip>
        ),
      },
    ],
    [],
  );

  const sources = Array.from(new Set((q.data ?? []).map((r) => r.source))).map((s) => ({ value: s, label: SOURCE_LABELS[s] ?? s }));
  const verdicts = Array.from(new Set((q.data ?? []).map((r) => r.verdict))).map((v) => ({ value: v, label: verdictLabel(v) }));
  const editorHref = (r: QueryHistoryItem) => {
    const sp = new URLSearchParams({ sql: r.executed_sql ?? "" });
    const ds = historyDatasetId(r);
    if (ds) sp.set("dataset", ds);
    return `/o/${slug}/ask/sql?${sp}`;
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/ask`} className="inline-flex items-center gap-1 normal-case tracking-normal hover:text-fg">
            <ArrowLeft className="size-3" /> Ask AI
          </Link>
        }
        title="Query history"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Every question and manual query from the guarded query audit log: source, guard verdict, rows and duration."
      />
      {q.isError && isUnavailableEndpoint(q.error) ? (
        <Card>
          <EmptyState icon={<History />} title="Query history isn't available yet" description="It appears once the Conversational BI service is deployed." />
        </Card>
      ) : (
        <DataTable
          caption="Query history"
          columns={columns}
          data={q.data}
          loading={q.isLoading}
          error={q.isError ? q.error : undefined}
          onRetry={() => void q.refetch()}
          searchPlaceholder="Search questions and SQL…"
          filters={[
            { columnId: "source", label: "Source", options: sources },
            { columnId: "verdict", label: "Verdict", options: verdicts },
          ]}
          initialSorting={[{ id: "occurred_at", desc: true }]}
          onRowClick={setOpen}
          getRowId={(r) => r.id}
          emptyState={
            <EmptyState
              compact
              icon={<History />}
              title="No queries yet"
              description="Questions you ask in Ask AI and manual SQL runs are listed here."
              action={
                <Button size="sm" asChild>
                  <Link href={`/o/${slug}/ask`}>Ask a question</Link>
                </Button>
              }
            />
          }
        />
      )}
      <Dialog open={!!open} onOpenChange={(o) => !o && setOpen(null)}>
        <DialogContent size="lg">
          {open && (
            <>
              <DialogHeader title={open.question ?? "Manual SQL"} description={`${historyDatasetName(open)} · ${formatDateTime(open.occurred_at)}${open.user_email ? ` · ${open.user_email}` : ""}`} />
              <DialogBody className="space-y-3">
                <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                  <SourceBadge source={open.source} />
                  <VerdictBadge verdict={open.verdict} />
                  <span className="tabular">{formatNumber(open.row_count)} rows</span>
                  <span className="tabular">· {formatMs(open.duration_ms)}</span>
                </div>
                {open.reject_code && (
                  <p className="rounded-control bg-danger-soft px-3 py-2 text-sm text-danger">
                    <span className="font-mono">{open.reject_code}</span>: {guardMessage(open.reject_code) ?? "Rejected by the SQL guard."}
                  </p>
                )}
                {open.executed_sql ? <SqlCode sql={open.executed_sql} className="max-h-80" /> : <p className="text-sm text-fg-muted">No SQL was executed.</p>}
              </DialogBody>
              <DialogFooter>
                {can("bi.sql") && open.executed_sql && (
                  <Button variant="secondary" asChild>
                    <Link href={editorHref(open)}>
                      <SquareTerminal /> Open in SQL editor
                    </Link>
                  </Button>
                )}
                <Button onClick={() => setOpen(null)}>Close</Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
