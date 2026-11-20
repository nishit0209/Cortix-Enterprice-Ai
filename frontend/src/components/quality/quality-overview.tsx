"use client";

import * as React from "react";
import Link from "next/link";
import { useQueries } from "@tanstack/react-query";
import { Copy, Gauge, ListChecks, ShieldAlert, TriangleAlert, Upload } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState, ErrorState } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { ScoreRing, scoreTone } from "@/components/ui/score-ring";
import { StatCard } from "@/components/ui/stat-card";
import { ClassificationBadge, FeatureStatusBadge } from "@/components/ui/status";
import { api } from "@/lib/api";
import { datasetKeys, useDatasets } from "@/lib/queries/datasets";
import type { Dataset, QualityReport, QualityRule } from "@/lib/types";
import { cn, formatNumber, titleCase, toPercentScore } from "@/lib/utils";

const MAX_REPORTS = 30;

interface FailingRule extends QualityRule {
  datasetId: string;
  datasetName: string;
}

const SEVERITY_TONE: Record<string, "danger" | "warning" | "neutral"> = { error: "danger", critical: "danger", warn: "warning", warning: "warning", info: "neutral" };

export function QualityOverview() {
  const { slug, can } = useOrgContext();
  const datasets = useDatasets(slug);
  const ready = React.useMemo(() => (datasets.data ?? []).filter((d) => String(d.status) === "ready").slice(0, MAX_REPORTS), [datasets.data]);
  const reports = useQueries({
    queries: ready.map((d) => ({
      queryKey: datasetKeys.quality(slug, d.id),
      queryFn: ({ signal }: { signal: AbortSignal }) => api.get<QualityReport>(`/orgs/${encodeURIComponent(slug)}/datasets/${d.id}/quality`, { signal }),
      staleTime: 60_000,
      retry: false,
    })),
  });

  const rows = ready.map((d, i) => ({ dataset: d, report: reports[i]?.data, loading: reports[i]?.isLoading }));
  const scored = rows.map((r) => ({ ...r, score: toPercentScore(r.report?.score ?? r.dataset.quality_score) }));
  const withScore = scored.filter((r) => r.score !== null);
  const avg = withScore.length ? withScore.reduce((a, r) => a + (r.score ?? 0), 0) / withScore.length : null;
  const below = withScore.filter((r) => (r.score ?? 100) < 70).length;
  const failing: FailingRule[] = rows.flatMap((r) =>
    (r.report?.rules ?? []).filter((x) => !x.passed).map((x) => ({ ...x, datasetId: r.dataset.id, datasetName: r.dataset.name })),
  );
  const dupes = rows.reduce((a, r) => a + (r.report?.duplicates?.exact_rows ?? 0), 0);
  const loadingReports = reports.some((r) => r.isLoading);

  const ruleColumns = React.useMemo<DataTableColumn<FailingRule>[]>(
    () => [
      {
        id: "dataset",
        accessorKey: "datasetName",
        header: "Dataset",
        cell: ({ row }) => (
          <Link href={`/o/${slug}/data/datasets/${row.original.datasetId}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
            {row.original.datasetName}
          </Link>
        ),
      },
      { id: "column", accessorFn: (r) => r.column ?? "(table)", header: "Column", cell: ({ row }) => <span className="font-mono text-xs">{row.original.column ?? "(table)"}</span> },
      { id: "rule_type", accessorKey: "rule_type", header: "Rule", cell: ({ row }) => titleCase(row.original.rule_type) },
      {
        id: "severity",
        accessorKey: "severity",
        header: "Severity",
        cell: ({ row }) => <Badge tone={SEVERITY_TONE[row.original.severity] ?? "neutral"}>{titleCase(row.original.severity)}</Badge>,
      },
      { id: "failed_count", accessorKey: "failed_count", header: "Failing rows", meta: { align: "right" }, cell: ({ row }) => formatNumber(row.original.failed_count) },
    ],
    [slug],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Data quality"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Quality scores across your datasets (0–100: rule pass rate plus completeness, uniqueness, validity and consistency) and every failing rule."
      />
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Quality summary">
        <StatCard label="Average score" icon={<Gauge />} loading={datasets.isLoading} value={avg === null ? "—" : Math.round(avg)} hint={`${withScore.length} scored dataset${withScore.length === 1 ? "" : "s"}`} accent={avg !== null && scoreTone(avg) === "success" ? "success" : "warning"} />
        <StatCard label="Below 70" icon={<ShieldAlert />} loading={datasets.isLoading} value={formatNumber(below)} hint="Datasets that need attention" accent="warning" />
        <StatCard label="Failing rules" icon={<TriangleAlert />} loading={datasets.isLoading || loadingReports} value={formatNumber(failing.length)} hint={`${failing.filter((f) => f.severity === "error").length} at error severity`} accent="warning" />
        <StatCard label="Exact duplicate rows" icon={<Copy />} loading={datasets.isLoading || loadingReports} value={formatNumber(dupes)} hint="Across scored datasets" accent="teal" />
      </section>

      {datasets.isError ? (
        <ErrorState error={datasets.error} onRetry={() => void datasets.refetch()} />
      ) : datasets.isSuccess && ready.length === 0 ? (
        <Card>
          <EmptyState
            icon={<ListChecks />}
            title="No profiled datasets yet"
            description="Quality is computed when a dataset finishes ingesting. Upload a dataset to get its first score."
            action={
              can("datasets.create") ? (
                <Button size="sm" asChild>
                  <Link href={`/o/${slug}/data/datasets?upload=1`}>
                    <Upload /> Upload a dataset
                  </Link>
                </Button>
              ) : undefined
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          <Card>
            <CardHeader title="Scores by dataset" description="Lowest first" />
            <CardContent>
              <ul className="space-y-2.5">
                {[...scored]
                  .sort((a, b) => (a.score ?? 101) - (b.score ?? 101))
                  .map(({ dataset, report, score }) => (
                    <DatasetScoreRow key={dataset.id} slug={slug} dataset={dataset} report={report} score={score} />
                  ))}
              </ul>
            </CardContent>
          </Card>
          <div className="space-y-3">
            <h2 className="text-sm font-semibold text-fg">Failing rules</h2>
            <DataTable
              caption="Failing quality rules"
              columns={ruleColumns}
              data={failing}
              loading={datasets.isLoading || loadingReports}
              getRowId={(r) => `${r.datasetId}:${r.id}`}
              initialSorting={[{ id: "failed_count", desc: true }]}
              filters={[
                { columnId: "severity", label: "Severity", options: Array.from(new Set(failing.map((f) => f.severity))).map((s) => ({ value: s, label: titleCase(s) })) },
              ]}
              emptyState={<EmptyState compact icon={<ListChecks />} title="All rules pass" description="No failing quality rules across your datasets." />}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function DatasetScoreRow({ slug, dataset, report, score }: { slug: string; dataset: Dataset; report: QualityReport | undefined; score: number | null }) {
  const dims = report?.dimensions;
  const failing = (report?.rules ?? []).filter((r) => !r.passed).length;
  return (
    <li>
      <Link href={`/o/${slug}/data/datasets/${dataset.id}`} className="flex items-center gap-3 rounded-control px-2 py-2 transition-colors hover:bg-surface-2 focus-ring">
        <ScoreRing score={score} size={40} label={`${dataset.name} quality`} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-fg">{dataset.name}</span>
            <ClassificationBadge value={dataset.classification} />
          </div>
          {dims ? (
            <div className="mt-1 grid grid-cols-4 gap-1.5" aria-label="Dimensions">
              {(["completeness", "uniqueness", "validity", "consistency"] as const).map((k) => {
                const v = toPercentScore(dims[k]);
                return (
                  <div key={k} title={`${titleCase(k)}: ${v === null ? "—" : Math.round(v)}`}>
                    <div className="h-1 overflow-hidden rounded-full bg-surface-3">
                      <div className={cn("h-full rounded-full", v === null ? "" : v >= 90 ? "bg-success" : v >= 70 ? "bg-warning" : "bg-danger")} style={{ width: `${v ?? 0}%` }} />
                    </div>
                    <div className="mt-0.5 truncate text-[10px] text-fg-subtle">{k.slice(0, 5)}</div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-xs text-fg-subtle">No report</div>
          )}
        </div>
        {failing > 0 && <Badge tone="warning">{failing} failing</Badge>}
      </Link>
    </li>
  );
}
