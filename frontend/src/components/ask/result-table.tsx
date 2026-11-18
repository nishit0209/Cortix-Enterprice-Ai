"use client";

import * as React from "react";
import { EyeOff, Table2 } from "lucide-react";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/feedback";
import { Tooltip } from "@/components/ui/overlays";
import { normalizeColumns } from "@/lib/chat/chart";
import type { ResultColumn } from "@/lib/types";

type Row = { __i: number; cells: unknown[] };

export function ResultCell({ value, masked }: { value: unknown; masked?: boolean }) {
  if (masked) {
    return (
      <Tooltip content="Masked by your access policy">
        <span className="inline-flex items-center gap-1 text-fg-subtle">
          <EyeOff className="size-3" aria-hidden />
          •••
        </span>
      </Tooltip>
    );
  }
  if (value === null || value === undefined) return <span className="text-fg-subtle">null</span>;
  if (typeof value === "boolean") return <span className="font-mono text-xs">{String(value)}</span>;
  if (typeof value === "number") return <span className="tabular">{value.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>;
  if (typeof value === "object") return <span className="font-mono text-xs">{JSON.stringify(value)}</span>;
  return (
    <span className="block max-w-[320px] truncate" title={String(value)}>
      {String(value)}
    </span>
  );
}

/** Query result grid (sortable, searchable, paginated; masked cells rendered as •••). */
export function ResultTable({
  columns,
  rows,
  caption,
  pageSize = 10,
  footer,
}: {
  columns: Array<ResultColumn | string>;
  rows: unknown[][];
  caption: string;
  pageSize?: number;
  footer?: React.ReactNode;
}) {
  const cols = React.useMemo(() => normalizeColumns(columns, rows), [columns, rows]);
  const defs = React.useMemo<DataTableColumn<Row>[]>(
    () =>
      cols.map((c, i) => ({
        id: `c${i}`,
        header: () => (
          <span className="inline-flex items-center gap-1.5">
            <span className="font-mono">{c.name}</span>
            {c.masked && <EyeOff className="size-3 text-fg-subtle" aria-label="masked" />}
          </span>
        ),
        accessorFn: (r: Row) => r.cells[i] as string | number | null,
        meta: { label: c.name, align: c.kind === "number" ? ("right" as const) : undefined },
        enableSorting: !c.masked,
        cell: ({ row }: { row: { original: Row } }) => <ResultCell value={row.original.cells[i]} masked={c.masked} />,
      })),
    [cols],
  );
  const data = React.useMemo(() => rows.map((cells, i) => ({ __i: i, cells })), [rows]);
  return (
    <DataTable
      caption={caption}
      columns={defs}
      data={data}
      dense
      pageSize={pageSize}
      pageSizes={[10, 25, 50, 100]}
      searchPlaceholder="Search rows…"
      columnToggle={cols.length > 4}
      emptyState={<EmptyState compact icon={<Table2 />} title="No rows" description="The query returned no rows." />}
      footer={footer}
    />
  );
}
