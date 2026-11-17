"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Database, Upload } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/feedback";
import { ClassificationBadge, FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { scoreTone } from "@/components/ui/score-ring";
import { Tooltip } from "@/components/ui/overlays";
import { useDatasets } from "@/lib/queries/datasets";
import { isUnavailableEndpoint } from "@/lib/errors";
import { cn, formatDateTime, formatNumber, formatRelative, toPercentScore } from "@/lib/utils";
import type { Dataset } from "@/lib/types";
import { UploadDatasetDialog } from "./upload-dialog";
import { CLASSIFICATIONS } from "./classification";

const EMPTY: Dataset[] = [];

export function QualityPill({ score }: { score: number | null | undefined }) {
  const v = toPercentScore(score);
  if (v === null) return <span className="text-fg-subtle">—</span>;
  const tone = scoreTone(v);
  return (
    <span className="inline-flex items-center gap-2">
      <span className="h-1.5 w-12 overflow-hidden rounded-full bg-surface-3" aria-hidden>
        <span
          className={cn("block h-full rounded-full", tone === "success" ? "bg-success" : tone === "warning" ? "bg-warning" : "bg-danger")}
          style={{ width: `${v}%` }}
        />
      </span>
      <span className="tabular w-7 text-right font-medium">{Math.round(v)}</span>
    </span>
  );
}

export function DatasetsView() {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const q = useDatasets(slug);
  const [uploadOpen, setUploadOpen] = React.useState(false);

  // Deep link: ?upload=1 (command menu, dashboard quick actions) opens the upload dialog.
  React.useEffect(() => {
    if (params.get("upload") === "1" && can("datasets.create")) {
      setUploadOpen(true);
      router.replace(pathname, { scroll: false });
    }
  }, [params, can, router, pathname]);

  const columns = React.useMemo<DataTableColumn<Dataset>[]>(
    () => [
      {
        accessorKey: "name",
        header: "Name",
        meta: { label: "Name" },
        cell: ({ row }) => (
          <div className="min-w-0 max-w-[340px]">
            <Link
              href={`/o/${slug}/data/datasets/${row.original.id}`}
              className="block truncate font-medium text-fg hover:text-accent"
              onClick={(e) => e.stopPropagation()}
            >
              {row.original.name}
            </Link>
            {row.original.description && <p className="truncate text-xs text-fg-muted">{row.original.description}</p>}
          </div>
        ),
      },
      {
        accessorKey: "classification",
        header: "Classification",
        cell: ({ getValue }) => <ClassificationBadge value={getValue() as string} />,
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={getValue() as string} />,
      },
      {
        accessorKey: "row_count",
        header: "Rows",
        meta: { align: "right" },
        cell: ({ getValue }) => formatNumber(getValue() as number | null),
      },
      {
        accessorKey: "column_count",
        header: "Columns",
        meta: { align: "right" },
        cell: ({ getValue }) => formatNumber(getValue() as number | null),
      },
      {
        accessorKey: "quality_score",
        header: "Quality",
        meta: { align: "right" },
        cell: ({ getValue }) => <QualityPill score={getValue() as number | null} />,
      },
      {
        accessorKey: "updated_at",
        header: "Updated",
        meta: { align: "right" },
        cell: ({ getValue }) => (
          <Tooltip content={formatDateTime(getValue() as string)}>
            <span className="whitespace-nowrap text-fg-muted">{formatRelative(getValue() as string)}</span>
          </Tooltip>
        ),
      },
    ],
    [slug],
  );

  const statusOptions = React.useMemo(() => {
    const set = new Set((q.data ?? []).map((d) => String(d.status)));
    return [...set].sort().map((s) => ({ label: s.charAt(0).toUpperCase() + s.slice(1), value: s }));
  }, [q.data]);

  const filters = React.useMemo(
    () => [
      {
        columnId: "classification",
        label: "Class",
        options: CLASSIFICATIONS.map((c) => ({ label: c.label, value: c.value })),
      },
      ...(statusOptions.length > 1 ? [{ columnId: "status", label: "Status", options: statusOptions }] : []),
    ],
    [statusOptions],
  );

  const unavailable = q.isError && isUnavailableEndpoint(q.error);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Datasets"
        badge={<FeatureStatusBadge status="working" />}
        description="Every uploaded file becomes a versioned dataset with a profile, PII tags and a quality score."
        actions={
          can("datasets.create") ? (
            <Button onClick={() => setUploadOpen(true)}>
              <Upload /> Upload dataset
            </Button>
          ) : undefined
        }
      />
      <DataTable
        caption="Datasets"
        columns={columns}
        data={unavailable ? EMPTY : q.data}
        loading={q.isLoading}
        error={q.isError && !unavailable ? q.error : undefined}
        onRetry={() => void q.refetch()}
        searchPlaceholder="Search datasets…"
        initialSorting={[{ id: "updated_at", desc: true }]}
        filters={filters}
        getRowId={(d) => d.id}
        onRowClick={(d) => router.push(`/o/${slug}/data/datasets/${d.id}`)}
        emptyState={
          unavailable ? (
            <EmptyState
              icon={<Database />}
              title="The datasets service isn't available yet"
              description="Uploads and dataset profiles appear here once the data service is deployed."
            />
          ) : (
            <EmptyState
              icon={<Database />}
              title="No datasets yet"
              description="Upload a CSV, TSV, XLSX or JSON file. NEXORA detects types, profiles every column and scores quality."
              action={
                can("datasets.create") ? (
                  <Button onClick={() => setUploadOpen(true)}>
                    <Upload /> Upload your first dataset
                  </Button>
                ) : undefined
              }
            />
          )
        }
      />
      <UploadDatasetDialog org={slug} open={uploadOpen} onOpenChange={setUploadOpen} />
    </div>
  );
}
