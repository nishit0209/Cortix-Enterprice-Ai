"use client";

import * as React from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  GitBranch,
  Sparkles,
  Columns3,
  Copy,
  Database,
  EyeOff,
  Layers,
  ListChecks,
  Loader2,
  MoreHorizontal,
  Pencil,
  Rows3,
  Table2,
  Trash2,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Tooltip,
} from "@/components/ui/overlays";
import { ScoreRing } from "@/components/ui/score-ring";
import { ClassificationBadge, StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  isDatasetProcessing,
  useDataset,
  useDatasetColumns,
  useDatasetPreview,
  useDatasetQuality,
  useDeleteDataset,
} from "@/lib/queries/datasets";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { cn, formatDateTime, formatNumber, formatRelative, titleCase, toPercentScore } from "@/lib/utils";
import type { Dataset, DatasetPreview, QualityReport, QualityRule } from "@/lib/types";
import { ColumnProfileCard } from "./profile";
import { ClassifyDialog } from "./classify-dialog";
import { SemanticTab } from "./semantic-tab";
import { LineageGraphView } from "@/components/lineage/lineage-graph";

export function DatasetDetail() {
  const { id } = useParams<{ id: string }>();
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const q = useDataset(slug, id);
  const [tab, setTab] = React.useState("preview");
  const [classifyOpen, setClassifyOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  const del = useDeleteDataset(slug);
  useSetBreadcrumb(q.data?.name);

  if (q.isLoading) return <DetailSkeleton />;
  if (q.isError || !q.data) {
    return (
      <Card>
        {q.error && (q.error as { status?: number }).status === 404 ? (
          <EmptyState
            icon={<Database />}
            title="Dataset not found"
            description="It may have been deleted, or it belongs to another organization."
            action={
              <Button variant="secondary" asChild>
                <Link href={`/o/${slug}/data/datasets`}>
                  <ArrowLeft /> Back to datasets
                </Link>
              </Button>
            }
          />
        ) : (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        )}
      </Card>
    );
  }

  const d = q.data;
  const processing = isDatasetProcessing(d);
  const ready = !processing && d.status !== "failed";

  return (
    <div className="space-y-6">
      <div>
        <Link
          href={`/o/${slug}/data/datasets`}
          className="inline-flex items-center gap-1.5 rounded-[4px] text-sm text-fg-muted transition-colors hover:text-fg focus-ring"
        >
          <ArrowLeft className="size-4" /> Datasets
        </Link>
      </div>

      <Card className="overflow-hidden">
        <div className="flex flex-col gap-5 p-5 sm:p-6 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex min-w-0 items-start gap-4">
            <span className="flex size-11 shrink-0 items-center justify-center rounded-card bg-accent-soft text-accent-soft-fg">
              <Database className="size-5" aria-hidden />
            </span>
            <div className="min-w-0 space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="truncate text-xl font-semibold tracking-[-0.015em] text-fg">{d.name}</h1>
                <StatusBadge status={String(d.status)} />
              </div>
              {d.description && <p className="max-w-2xl text-sm text-fg-muted">{d.description}</p>}
              <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                <Tooltip content="Classification controls AI eligibility under your org's data policy">
                  <span>
                    <ClassificationBadge value={d.classification} />
                  </span>
                </Tooltip>
                {d.current_version && <Badge tone="outline">v{d.current_version.version}</Badge>}
                {(d.tags ?? []).map((t) => (
                  <Badge key={t} tone="neutral">
                    {t}
                  </Badge>
                ))}
                <span className="text-fg-subtle">·</span>
                <Tooltip content={formatDateTime(d.updated_at)}>
                  <span>Updated {formatRelative(d.updated_at)}</span>
                </Tooltip>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-5 lg:gap-6">
            <Metric icon={<Rows3 />} label="Rows" value={formatNumber(d.row_count)} />
            <Metric icon={<Columns3 />} label="Columns" value={formatNumber(d.column_count)} />
            <div className="flex items-center gap-3">
              <ScoreRing score={toPercentScore(d.quality_score)} size={52} label="Data quality" />
              <div className="text-xs text-fg-muted">
                Quality
                <br />
                score
              </div>
            </div>
            {ready && can("bi.chat") && (
              <Button variant="secondary" size="sm" asChild>
                <Link href={`/o/${slug}/ask?dataset=${d.id}`}>
                  <Sparkles /> Ask AI
                </Link>
              </Button>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="secondary" size="icon-sm" aria-label="Dataset actions">
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {can("datasets.classify") && (
                  <DropdownMenuItem onSelect={() => setClassifyOpen(true)}>
                    <Pencil /> Change classification
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  onSelect={() => {
                    void navigator.clipboard?.writeText(d.id);
                    toast.success("Dataset ID copied");
                  }}
                >
                  <Copy /> Copy dataset ID
                </DropdownMenuItem>
                {d.current_version?.wh_table && (
                  <DropdownMenuItem
                    onSelect={() => {
                      void navigator.clipboard?.writeText(d.current_version!.wh_table!);
                      toast.success("Warehouse table copied");
                    }}
                  >
                    <Table2 /> Copy warehouse table
                  </DropdownMenuItem>
                )}
                {can("datasets.manage") && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem destructive onSelect={() => setDeleteOpen(true)}>
                      <Trash2 /> Delete dataset
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        {processing && (
          <div className="flex items-center gap-2.5 border-t border-border bg-info-soft/60 px-6 py-2.5 text-sm text-info" role="status">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            This dataset is still being processed. Profile and quality appear automatically when ready.
          </div>
        )}
        {d.status === "failed" && (
          <div className="flex items-center gap-2.5 border-t border-border bg-danger-soft px-6 py-2.5 text-sm text-danger" role="alert">
            <XCircle className="size-4" aria-hidden />
            Processing failed. Upload the file again, or check the audit log for details.
          </div>
        )}
      </Card>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="Dataset views">
          <TabsTrigger value="preview">
            <Table2 /> Preview
          </TabsTrigger>
          <TabsTrigger value="schema">
            <Layers /> Schema &amp; profile
          </TabsTrigger>
          <TabsTrigger value="quality">
            <ListChecks /> Quality
          </TabsTrigger>
          <TabsTrigger value="semantic">
            <BookOpen /> Semantic
          </TabsTrigger>
          <TabsTrigger value="lineage">
            <GitBranch /> Lineage
          </TabsTrigger>
        </TabsList>
        <TabsContent value="preview">
          <PreviewTab org={slug} dataset={d} enabled={tab === "preview" && ready} processing={processing} />
        </TabsContent>
        <TabsContent value="schema">
          <SchemaTab org={slug} dataset={d} enabled={tab === "schema" && ready} processing={processing} />
        </TabsContent>
        <TabsContent value="quality">
          <QualityTab org={slug} dataset={d} enabled={tab === "quality" && ready} processing={processing} />
        </TabsContent>
        <TabsContent value="semantic">
          {processing ? <ProcessingState /> : tab === "semantic" ? <SemanticTab org={slug} dataset={d} canManage={can("semantic.manage")} /> : null}
        </TabsContent>
        <TabsContent value="lineage">
          {tab === "lineage" && (
            <Card>
              <CardContent className="pt-5">
                <LineageGraphView org={slug} nodeType="dataset" nodeId={d.id} height={440} />
              </CardContent>
            </Card>
          )}
        </TabsContent>
      </Tabs>

      <ClassifyDialog org={slug} dataset={d} open={classifyOpen} onOpenChange={setClassifyOpen} />
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete “${d.name}”?`}
        description="The dataset, its versions, profile and warehouse table are removed. This cannot be undone."
        confirmLabel="Delete dataset"
        confirmText={d.name}
        loading={del.isPending}
        onConfirm={async () => {
          try {
            await del.mutateAsync(d.id);
            toast.success("Dataset deleted");
            router.replace(`/o/${slug}/data/datasets`);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2.5">
      <span className="flex size-8 items-center justify-center rounded-control bg-surface-2 text-fg-subtle [&_svg]:size-4" aria-hidden>
        {icon}
      </span>
      <div>
        <div className="tabular text-md font-semibold leading-5 text-fg">{value}</div>
        <div className="text-xs text-fg-muted">{label}</div>
      </div>
    </div>
  );
}

function ProcessingState() {
  return (
    <Card>
      <EmptyState
        icon={<Loader2 className="animate-spin" />}
        title="Still processing"
        description="This view becomes available once ingestion and profiling finish."
      />
    </Card>
  );
}

function UnavailableOr({ error, onRetry, what }: { error: unknown; onRetry: () => void; what: string }) {
  return (
    <Card>
      {isUnavailableEndpoint(error) ? (
        <EmptyState icon={<Database />} title={`${what} isn't available`} description="Nothing has been computed for this dataset yet." />
      ) : (
        <ErrorState error={error} onRetry={onRetry} />
      )}
    </Card>
  );
}

/* ---------------- Preview ---------------- */

type PreviewRow = { __i: number; cells: unknown[] };

function renderCell(v: unknown, masked: boolean) {
  if (masked) {
    return (
      <Tooltip content="Masked by policy">
        <span className="inline-flex items-center gap-1 text-fg-subtle">
          <EyeOff className="size-3" aria-hidden />
          •••
        </span>
      </Tooltip>
    );
  }
  if (v === null || v === undefined || v === "") return <span className="text-fg-subtle">null</span>;
  if (typeof v === "boolean") return <span className="font-mono text-xs">{String(v)}</span>;
  if (typeof v === "number") return <span className="tabular">{v.toLocaleString()}</span>;
  if (typeof v === "object") return <span className="font-mono text-xs">{JSON.stringify(v)}</span>;
  return <span className="block max-w-[280px] truncate" title={String(v)}>{String(v)}</span>;
}

function PreviewTab({ org, dataset, enabled, processing }: { org: string; dataset: Dataset; enabled: boolean; processing: boolean }) {
  const q = useDatasetPreview(org, dataset.id, enabled);
  const preview: DatasetPreview | undefined = q.data;

  const columns = React.useMemo<DataTableColumn<PreviewRow>[]>(
    () =>
      (preview?.columns ?? []).map((c, i) => ({
        id: `c${i}`,
        header: () => (
          <span className="inline-flex items-center gap-1.5">
            <span className="font-mono">{c.name}</span>
            <span className="font-normal text-fg-subtle">{c.type}</span>
          </span>
        ),
        accessorFn: (row: PreviewRow) => row.cells[i] as string | number | null,
        meta: {
          label: c.name,
          align: /(int|float|double|decimal|numeric)/i.test(c.type) ? ("right" as const) : undefined,
        },
        enableSorting: !c.masked,
        cell: ({ row }: { row: { original: PreviewRow } }) => renderCell(row.original.cells[i], c.masked),
      })),
    [preview?.columns],
  );
  const rows = React.useMemo(() => (preview?.rows ?? []).map((cells, i) => ({ __i: i, cells })), [preview?.rows]);

  if (processing) return <ProcessingState />;
  if (q.isError) return <UnavailableOr error={q.error} onRetry={() => void q.refetch()} what="Preview" />;

  const maskedCount = (preview?.columns ?? []).filter((c) => c.masked).length;
  return (
    <div className="space-y-3">
      {maskedCount > 0 && (
        <p className="flex items-center gap-2 text-xs text-fg-muted">
          <EyeOff className="size-3.5" aria-hidden />
          {maskedCount} column{maskedCount > 1 ? "s are" : " is"} masked by your access policy.
        </p>
      )}
      <DataTable
        caption={`Preview of ${dataset.name}`}
        columns={columns}
        data={rows}
        loading={q.isLoading || !enabled}
        dense
        pageSize={25}
        searchPlaceholder="Search preview rows…"
        emptyState={<EmptyState compact icon={<Table2 />} title="No rows" description="This dataset has no rows to preview." />}
        footer={
          preview ? (
            <p className="text-xs text-fg-subtle">
              Showing the first {formatNumber(preview.rows.length)} of {formatNumber(dataset.row_count)} rows.
            </p>
          ) : undefined
        }
      />
    </div>
  );
}

/* ---------------- Schema & profile ---------------- */

function SchemaTab({ org, dataset, enabled, processing }: { org: string; dataset: Dataset; enabled: boolean; processing: boolean }) {
  const q = useDatasetColumns(org, dataset.id, enabled);
  const [filter, setFilter] = React.useState<"all" | "pii">("all");
  if (processing) return <ProcessingState />;
  if (q.isError) return <UnavailableOr error={q.error} onRetry={() => void q.refetch()} what="The profile" />;
  if (q.isLoading || !enabled) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-56 rounded-card" />
        ))}
      </div>
    );
  }
  const cols = q.data ?? [];
  if (cols.length === 0) {
    return (
      <Card>
        <EmptyState icon={<Layers />} title="No columns profiled" description="The profile appears after ingestion completes." />
      </Card>
    );
  }
  const pii = cols.filter((c) => c.is_pii).length;
  const shown = filter === "pii" ? cols.filter((c) => c.is_pii) : cols;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-fg-muted">
          <span className="tabular font-medium text-fg">{cols.length}</span> columns ·{" "}
          <span className={cn("tabular font-medium", pii > 0 ? "text-danger" : "text-fg")}>{pii}</span> with PII detected
        </p>
        <div className="flex rounded-control border border-border p-0.5 text-xs" role="group" aria-label="Column filter">
          {(["all", "pii"] as const).map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={filter === f}
              onClick={() => setFilter(f)}
              className={cn(
                "rounded-[4px] px-2.5 py-1 font-medium transition-colors focus-ring",
                filter === f ? "bg-surface-2 text-fg" : "text-fg-muted hover:text-fg",
              )}
            >
              {f === "all" ? "All columns" : "PII only"}
            </button>
          ))}
        </div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {shown.map((c) => (
          <ColumnProfileCard key={c.name} column={c} />
        ))}
      </div>
    </div>
  );
}

/* ---------------- Quality ---------------- */

const DIMENSIONS: Array<{ key: keyof QualityReport["dimensions"]; label: string; hint: string }> = [
  { key: "completeness", label: "Completeness", hint: "Share of non-null values" },
  { key: "uniqueness", label: "Uniqueness", hint: "Absence of duplicate rows and keys" },
  { key: "validity", label: "Validity", hint: "Values matching their detected type and format" },
  { key: "consistency", label: "Consistency", hint: "Agreement across related fields" },
];

function QualityTab({ org, dataset, enabled, processing }: { org: string; dataset: Dataset; enabled: boolean; processing: boolean }) {
  const q = useDatasetQuality(org, dataset.id, enabled);
  const rules = React.useMemo<DataTableColumn<QualityRule>[]>(
    () => [
      {
        accessorKey: "rule_type",
        header: "Rule",
        cell: ({ getValue }) => <span className="font-medium">{titleCase(String(getValue()))}</span>,
      },
      {
        accessorKey: "column",
        header: "Column",
        cell: ({ getValue }) =>
          getValue() ? <span className="font-mono text-xs">{String(getValue())}</span> : <span className="text-fg-subtle">Dataset</span>,
      },
      {
        accessorKey: "severity",
        header: "Severity",
        cell: ({ getValue }) => {
          const s = String(getValue());
          return (
            <Badge tone={s === "critical" || s === "error" ? "danger" : s === "warning" ? "warning" : "neutral"}>{titleCase(s)}</Badge>
          );
        },
      },
      {
        id: "result",
        accessorFn: (r) => (r.passed ? "passed" : "failed"),
        header: "Result",
        cell: ({ row }) =>
          row.original.passed ? (
            <span className="inline-flex items-center gap-1.5 text-success">
              <CheckCircle2 className="size-4" aria-hidden /> Passed
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 text-danger">
              <XCircle className="size-4" aria-hidden /> Failed
            </span>
          ),
      },
      {
        accessorKey: "failed_count",
        header: "Failing rows",
        meta: { align: "right" },
        cell: ({ getValue }) => formatNumber(getValue() as number),
      },
    ],
    [],
  );
  const filters = React.useMemo(
    () => [
      {
        columnId: "result",
        label: "Result",
        options: [
          { label: "Passed", value: "passed" },
          { label: "Failed", value: "failed" },
        ],
      },
    ],
    [],
  );

  if (processing) return <ProcessingState />;
  if (q.isError) return <UnavailableOr error={q.error} onRetry={() => void q.refetch()} what="The quality report" />;
  const r = q.data;
  const loading = q.isLoading || !enabled;
  const score = toPercentScore(r?.score);
  const failed = (r?.rules ?? []).filter((x) => !x.passed).length;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-12">
        <Card className="flex items-center gap-5 p-5 lg:col-span-4">
          {loading ? (
            <Skeleton className="size-24 rounded-full" />
          ) : (
            <ScoreRing score={score} size={96} stroke={8} label="Overall quality" />
          )}
          <div className="space-y-1">
            <div className="text-sm font-semibold text-fg">Overall quality</div>
            {loading ? (
              <Skeleton className="h-3 w-32" />
            ) : (
              <p className="text-sm text-fg-muted">
                {(r?.rules ?? []).length} rules evaluated
                {failed > 0 ? (
                  <>
                    , <span className="font-medium text-danger">{failed} failing</span>
                  </>
                ) : (
                  ", all passing"
                )}
                .
              </p>
            )}
          </div>
        </Card>
        <Card className="p-5 lg:col-span-8">
          <div className="grid gap-4 sm:grid-cols-2">
            {DIMENSIONS.map((dim) => {
              const v = toPercentScore(r?.dimensions?.[dim.key] ?? null);
              return (
                <div key={dim.key} className="space-y-1.5">
                  <div className="flex items-center justify-between text-sm">
                    <Tooltip content={dim.hint}>
                      <span className="text-fg-muted">{dim.label}</span>
                    </Tooltip>
                    {loading ? <Skeleton className="h-3.5 w-8" /> : <span className="tabular font-medium text-fg">{v === null ? "—" : Math.round(v)}</span>}
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-surface-3">
                    <div
                      className={cn(
                        "h-full rounded-full transition-[width] duration-500",
                        v === null ? "" : v >= 85 ? "bg-success" : v >= 65 ? "bg-warning" : "bg-danger",
                      )}
                      style={{ width: `${v ?? 0}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <DataTable
            caption="Quality rules"
            columns={rules}
            data={r?.rules}
            loading={loading}
            filters={filters}
            searchPlaceholder="Search rules…"
            pageSize={10}
            columnToggle={false}
            initialSorting={[{ id: "failed_count", desc: true }]}
            emptyState={<EmptyState compact icon={<ListChecks />} title="No rules yet" description="Default rules are created during profiling." />}
          />
        </div>
        <Card className="lg:col-span-4">
          <CardHeader title="Duplicates" description="Detected during profiling" />
          <CardContent>
            {loading ? (
              <div className="space-y-3">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-full" />
              </div>
            ) : (
              <dl className="divide-y divide-border text-sm">
                <DupRow label="Exact duplicate rows" value={r?.duplicates?.exact_rows ?? 0} />
                <DupRow label="Duplicate keys" value={r?.duplicates?.key_duplicates ?? 0} />
                <DupRow label="Fuzzy clusters" value={(r?.duplicates?.fuzzy_clusters ?? []).length} />
              </dl>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function DupRow({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center justify-between py-2.5">
      <dt className="text-fg-muted">{label}</dt>
      <dd className={cn("tabular font-semibold", value > 0 ? "text-warning" : "text-success")}>{formatNumber(value)}</dd>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="space-y-6" aria-busy>
      <Skeleton className="h-4 w-24" />
      <Card className="flex items-center gap-4 p-6">
        <Skeleton className="size-11 rounded-card" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-6 w-64" />
          <Skeleton className="h-3.5 w-40" />
        </div>
        <Skeleton className="size-[52px] rounded-full" />
      </Card>
      <Skeleton className="h-10 w-80" />
      <Skeleton className="h-80 w-full rounded-card" />
    </div>
  );
}
