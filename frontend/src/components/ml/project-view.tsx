"use client";

import * as React from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { motion, useReducedMotion } from "framer-motion";
import { ArrowLeft, BarChart3, FlaskConical, Lightbulb, Play, RefreshCw, ShieldOff, Sparkles, Table2, Unplug, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { JobProgress } from "@/components/jobs/job-progress";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ChartCard } from "@/components/ui/chart-card";
import { EmptyState, ErrorState, Progress, Skeleton } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { StatCard } from "@/components/ui/stat-card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorMessage } from "@/lib/errors";
import { algorithmLabel } from "@/lib/ml/algorithms";
import type { Eda, MlProject, Recommendation } from "@/lib/ml/types";
import { useMlEda, useMlProject, useMlRecommendations, useRunEda } from "@/lib/queries/ml";
import { cn, formatDateTime, formatNumber, formatPercent } from "@/lib/utils";
import { MlError, TaskBadge } from "./common";
import { CorrelationHeatmap, CountBars, HBarChart, fmtMetric } from "./ml-charts";
import { TrainTab } from "./train-tab";
import { ExperimentsTab } from "./experiments-tab";

const TABS = ["eda", "recommendations", "train", "experiments"] as const;
type Tab = (typeof TABS)[number];

export function ProjectView() {
  const { id } = useParams<{ id: string }>();
  const { slug } = useOrgContext();
  const params = useSearchParams();
  const router = useRouter();
  const project = useMlProject(slug, id);
  useSetBreadcrumb(project.data?.name);
  const initial = (TABS as readonly string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "eda";
  const [tab, setTab] = React.useState<Tab>(initial);
  const [preset, setPreset] = React.useState<Recommendation[] | null>(null);

  const go = (t: Tab) => {
    setTab(t);
    router.replace(`/o/${slug}/ml/projects/${id}?tab=${t}`, { scroll: false });
  };

  if (project.isError) {
    return (
      <Card>
        <MlError error={project.error} what="This project" onRetry={() => void project.refetch()} />
      </Card>
    );
  }
  const p = project.data;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/ml`} className="inline-flex items-center gap-1 hover:text-fg">
            <ArrowLeft className="size-3" /> ML Workspace
          </Link>
        }
        title={p ? p.name : <Skeleton className="h-6 w-48" />}
        badge={p && <TaskBadge task={p.task_type} />}
        description={p?.description || undefined}
      >
        {p && (
          <p className="text-sm text-fg-muted">
            <Link href={`/o/${slug}/data/datasets/${p.dataset_id}`} className="font-medium text-fg hover:underline">
              {p.dataset_name ?? "Dataset"}
            </Link>
            {p.target_column && (
              <>
                {" "}
                → predicting <span className="font-mono text-fg">{p.target_column}</span>
              </>
            )}
            {p.time_column && (
              <>
                {" "}
                over <span className="font-mono text-fg">{p.time_column}</span>
              </>
            )}{" "}
            · {formatNumber(p.features.length)} features
          </p>
        )}
      </PageHeader>

      {p && (p.excluded_by_policy.length > 0 || p.leakage_dropped.length > 0) && <ExclusionsCard project={p} />}

      <Tabs value={tab} onValueChange={(v) => go(v as Tab)}>
        <TabsList aria-label="Project sections">
          <TabsTrigger value="eda">
            <BarChart3 /> EDA
          </TabsTrigger>
          <TabsTrigger value="recommendations">
            <Lightbulb /> Recommendations
          </TabsTrigger>
          <TabsTrigger value="train">
            <Play /> Train
          </TabsTrigger>
          <TabsTrigger value="experiments">
            <FlaskConical /> Experiments
          </TabsTrigger>
        </TabsList>
        <TabsContent value="eda">{p ? <EdaTab project={p} /> : <Skeleton className="h-64 rounded-card" />}</TabsContent>
        <TabsContent value="recommendations">
          {p && (
            <RecommendationsTab
              project={p}
              onTrain={(recs) => {
                setPreset(recs);
                go("train");
              }}
            />
          )}
        </TabsContent>
        <TabsContent value="train" forceMount hidden={tab !== "train"}>
          {p && <TrainTab project={p} preset={preset} onPresetConsumed={() => setPreset(null)} onShowExperiments={() => go("experiments")} />}
        </TabsContent>
        <TabsContent value="experiments">{p && <ExperimentsTab project={p} onTrain={() => go("train")} />}</TabsContent>
      </Tabs>
    </div>
  );
}

function ExclusionsCard({ project }: { project: MlProject }) {
  return (
    <Card>
      <CardContent className="grid gap-4 pt-4 md:grid-cols-2">
        {project.excluded_by_policy.length > 0 && (
          <div>
            <div className="mb-1.5 flex items-center gap-2 text-sm font-medium text-fg">
              <ShieldOff className="size-4 text-danger" /> Excluded by your data policy
            </div>
            <ul className="space-y-0.5 text-xs">
              {project.excluded_by_policy.map((r) => (
                <li key={r.column}>
                  <span className="font-mono text-fg">{r.column}</span> <span className="text-fg-muted">{r.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {project.leakage_dropped.length > 0 && (
          <div>
            <div className="mb-1.5 flex items-center gap-2 text-sm font-medium text-fg">
              <Unplug className="size-4 text-warning" /> Dropped to prevent target leakage
            </div>
            <ul className="space-y-0.5 text-xs">
              {project.leakage_dropped.map((r) => (
                <li key={r.column}>
                  <span className="font-mono text-fg">{r.column}</span> <span className="text-fg-muted">{r.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ---------------- EDA ---------------- */

function EdaTab({ project }: { project: MlProject }) {
  const { slug, can } = useOrgContext();
  const eda = useMlEda(slug, project.id);
  const run = useRunEda(slug, project.id);
  const [jobId, setJobId] = React.useState<string | null>(null);
  const canManage = can("ml.manage");

  const start = async () => {
    try {
      const res = await run.mutateAsync();
      if (res.job_id) setJobId(res.job_id);
      else void eda.refetch();
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const runBtn = canManage && (
    <Button variant={eda.data ? "secondary" : "primary"} size="sm" onClick={() => void start()} loading={run.isPending} disabled={!!jobId}>
      {eda.data ? <RefreshCw /> : <Wand2 />} {eda.data ? "Re-run EDA" : "Run EDA"}
    </Button>
  );

  return (
    <div className="space-y-5">
      {jobId && (
        <Card className="p-5">
          <JobProgress
            org={slug}
            jobId={jobId}
            title="Exploratory analysis"
            onDone={(s) => {
              setJobId(null);
              if (s.status === "succeeded") void eda.refetch();
            }}
          />
        </Card>
      )}
      {eda.isError ? (
        <Card>
          <MlError error={eda.error} what="EDA" onRetry={() => void eda.refetch()} />
        </Card>
      ) : eda.isLoading ? (
        <Skeleton className="h-64 rounded-card" />
      ) : !eda.data ? (
        <Card>
          <EmptyState
            icon={<BarChart3 />}
            title="No analysis yet"
            description="EDA runs locally on this server: column statistics, missingness, target balance, correlations and time coverage. No data leaves the org."
            action={runBtn || undefined}
          />
        </Card>
      ) : (
        <EdaReport eda={eda.data} actions={runBtn} />
      )}
    </div>
  );
}

function EdaReport({ eda, actions }: { eda: Eda; actions: React.ReactNode }) {
  const reduce = useReducedMotion();
  const missing = eda.columns
    .filter((c) => (c.missing_pct ?? 0) > 0)
    .map((c) => ({ feature: c.name, value: c.missing_pct ?? 0 }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 20);
  const categorical = eda.columns.filter((c) => c.top_values.length > 0).slice(0, 6);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-fg-muted">{eda.computed_at ? `Computed ${formatDateTime(eda.computed_at)}` : "Latest analysis"}</p>
        {actions}
      </div>
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="EDA summary">
        <StatCard label="Rows" value={formatNumber(eda.row_count)} />
        <StatCard label="Columns" value={formatNumber(eda.column_count)} accent="teal" />
        <StatCard label="Columns with gaps" value={formatNumber(eda.columns.filter((c) => (c.missing_pct ?? 0) > 0).length)} accent="warning" />
        <StatCard label="Duplicate rows" value={eda.duplicate_rows === null ? "—" : formatNumber(eda.duplicate_rows)} accent="success" />
      </section>

      {eda.narration && (
        <motion.div initial={reduce ? false : { opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}>
          <Card className="flex gap-3 p-4">
            <Sparkles className="mt-0.5 size-4 shrink-0 text-accent" />
            <div className="space-y-1">
              <p className="whitespace-pre-line text-sm text-fg">{eda.narration.text}</p>
              <p className="text-[11px] text-fg-subtle">{eda.narration.source === "gemini" ? "Narrated by Gemini from aggregates only, under your AI data policy." : "Generated locally from the statistics."}</p>
            </div>
          </Card>
        </motion.div>
      )}
      {eda.warnings.length > 0 && (
        <ul className="space-y-1 rounded-card border border-warning/30 bg-warning-soft/40 p-3 text-sm text-fg">
          {eda.warnings.map((w) => (
            <li key={w}>• {w}</li>
          ))}
        </ul>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <ChartCard
          title={eda.target ? `Target distribution · ${eda.target.name}` : "Target distribution"}
          description={eda.target?.kind === "categorical" ? "Class balance of the target." : "Histogram of the target."}
          empty={!eda.target || eda.target.distribution.length === 0 ? <EmptyState compact title="No target" description="Clustering projects have no target column." /> : undefined}
          table={{ columns: [{ key: "label", label: "Value" }, { key: "count", label: "Rows", align: "right", format: (v) => formatNumber(Number(v)) }], rows: eda.target?.distribution ?? [] }}
        >
          {eda.target && <CountBars data={eda.target.distribution} />}
        </ChartCard>
        <ChartCard
          title="Missing values"
          description="Share of empty cells per column (top 20)."
          empty={missing.length === 0 ? <EmptyState compact title="No missing values" description="Every column is complete." /> : undefined}
          table={{ columns: [{ key: "feature", label: "Column" }, { key: "value", label: "Missing %", align: "right", format: (v) => formatPercent(Number(v), 1) }], rows: missing }}
        >
          <HBarChart items={missing} color="var(--chart-3)" valueLabel="Missing %" format={(v) => `${v.toFixed(1)}%`} height={240} />
        </ChartCard>
      </div>

      {eda.correlation && (
        <Card>
          <CardHeader title="Correlation heatmap" description={`${eda.correlation.method === "spearman" ? "Spearman" : "Pearson"} correlation between numeric columns.`} />
          <CardContent>
            <CorrelationHeatmap columns={eda.correlation.columns} matrix={eda.correlation.matrix} />
          </CardContent>
        </Card>
      )}

      {eda.time_coverage && eda.time_coverage.series.length > 0 && (
        <ChartCard
          title={`Time coverage · ${eda.time_coverage.column}`}
          description={`${eda.time_coverage.start ?? "?"} to ${eda.time_coverage.end ?? "?"}`}
          table={{ columns: [{ key: "label", label: "Period" }, { key: "count", label: "Rows", align: "right", format: (v) => formatNumber(Number(v)) }], rows: eda.time_coverage.series.map((s) => ({ label: s.period, count: s.count })) }}
        >
          <CountBars data={eda.time_coverage.series.map((s) => ({ label: s.period, count: s.count }))} color="var(--chart-2)" />
        </ChartCard>
      )}

      {categorical.length > 0 && (
        <div>
          <h3 className="mb-3 text-sm font-semibold text-fg">Top categories</h3>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {categorical.map((c) => {
              const total = c.top_values.reduce((a, t) => a + t.count, 0);
              return (
                <Card key={c.name} className="p-4">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-[13px] text-fg">{c.name}</span>
                    {c.distinct !== null && <Badge>{formatNumber(c.distinct)} distinct</Badge>}
                  </div>
                  <ul className="space-y-1.5">
                    {c.top_values.slice(0, 6).map((t) => (
                      <li key={t.value} className="space-y-0.5 text-xs">
                        <div className="flex justify-between gap-2">
                          <span className="truncate text-fg-muted">{t.value || "(empty)"}</span>
                          <span className="tabular text-fg">{formatNumber(t.count)}</span>
                        </div>
                        <Progress value={total ? (t.count / total) * 100 : 0} label={`${c.name} ${t.value}`} />
                      </li>
                    ))}
                  </ul>
                </Card>
              );
            })}
          </div>
        </div>
      )}

      <Card>
        <CardHeader title="Column statistics" icon={<Table2 />} />
        <div className="scrollbar-thin overflow-x-auto px-5 pb-5">
          <table className="w-full min-w-[720px] text-sm">
            <caption className="sr-only">Column statistics</caption>
            <thead className="text-xs text-fg-muted">
              <tr className="border-b border-border">
                {["Column", "Type", "Missing", "Distinct", "Mean", "Std", "Min", "Median", "Max", "Outliers"].map((h, i) => (
                  <th key={h} className={cn("py-2 pr-3 font-medium", i > 1 ? "text-right" : "text-left")}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {eda.columns.map((c) => (
                <tr key={c.name} className="border-b border-border/60 last:border-0">
                  <td className="py-1.5 pr-3 font-mono text-[13px] text-fg">{c.name}</td>
                  <td className="py-1.5 pr-3 text-xs text-fg-muted">{c.dtype || c.kind}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{c.missing_pct === null ? "—" : formatPercent(c.missing_pct, 1)}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{formatNumber(c.distinct)}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{c.mean === null ? "—" : fmtMetric(c.mean)}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{c.std === null ? "—" : fmtMetric(c.std)}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{fmtCell(c.min)}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{c.median === null ? "—" : fmtMetric(c.median)}</td>
                  <td className="tabular py-1.5 pr-3 text-right">{fmtCell(c.max)}</td>
                  <td className="tabular py-1.5 text-right">{c.outliers === null ? "—" : formatNumber(c.outliers)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return fmtMetric(v);
  const s = String(v);
  return s.length > 16 ? `${s.slice(0, 15)}…` : s;
}

/* ---------------- recommendations ---------------- */

function RecommendationsTab({ project, onTrain }: { project: MlProject; onTrain: (recs: Recommendation[]) => void }) {
  const { slug, can } = useOrgContext();
  const recs = useMlRecommendations(slug, project.id);
  const reduce = useReducedMotion();
  if (recs.isError) {
    return (
      <Card>
        {recs.error && (recs.error as { status?: number }).status === 409 ? (
          <ErrorState compact error={recs.error} title="Run EDA first" />
        ) : (
          <MlError error={recs.error} what="Recommendations" onRetry={() => void recs.refetch()} />
        )}
      </Card>
    );
  }
  if (recs.isLoading)
    return (
      <div className="grid gap-4 md:grid-cols-2">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-40 rounded-card" />
        ))}
      </div>
    );
  const items = recs.data?.items ?? [];
  if (!items.length) {
    return (
      <Card>
        <EmptyState icon={<Lightbulb />} title="No recommendations yet" description="Recommendations are computed from the EDA profile. Run EDA first." />
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-fg-muted">
          Ranked by rules over your data&apos;s size, types, balance and goal{recs.data?.primary_metric ? (
            <>
              ; compared on <span className="font-mono text-fg">{recs.data.primary_metric}</span>
            </>
          ) : null}
          . You can override any choice.
        </p>
        {can("ml.manage") && (
          <Button size="sm" onClick={() => onTrain(items.slice(0, 3))}>
            <Play /> Train top {Math.min(3, items.length)}
          </Button>
        )}
      </div>
      {recs.data?.notes.length ? (
        <ul className="rounded-card border border-info/30 bg-info-soft/50 p-3 text-sm text-fg">
          {recs.data.notes.map((n) => (
            <li key={n}>• {n}</li>
          ))}
        </ul>
      ) : null}
      <ol className="grid gap-4 md:grid-cols-2">
        {items.map((r, i) => (
          <motion.li key={r.algorithm + i} initial={reduce ? false : { opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.05 }}>
            <Card className={cn("relative h-full p-5", i === 0 && "border-accent/50 shadow-soft")}>
              <div className="flex items-start gap-3">
                <span className={cn("flex size-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold", i === 0 ? "bg-accent text-accent-fg" : "bg-surface-2 text-fg-muted")}>{r.rank}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-semibold text-fg">{r.label === r.algorithm ? algorithmLabel(r.algorithm) : r.label}</h3>
                    {i === 0 && <Badge tone="accent">Top pick</Badge>}
                    {r.score !== null && <span className="tabular ml-auto text-xs text-fg-subtle">score {fmtMetric(r.score)}</span>}
                  </div>
                  <ul className="mt-2 space-y-1 text-sm text-fg-muted">
                    {r.reasons.map((reason) => (
                      <li key={reason} className="flex gap-2">
                        <span className="mt-2 size-1 shrink-0 rounded-full bg-accent" aria-hidden />
                        {reason}
                      </li>
                    ))}
                  </ul>
                  {r.warnings.length > 0 && <p className="mt-2 text-xs text-warning">{r.warnings.join(" ")}</p>}
                  {can("ml.manage") && (
                    <Button size="xs" variant="secondary" className="mt-3" onClick={() => onTrain([r])}>
                      <Play /> Train this
                    </Button>
                  )}
                </div>
              </div>
            </Card>
          </motion.li>
        ))}
      </ol>
    </div>
  );
}
