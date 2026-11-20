"use client";

import * as React from "react";
import Link from "next/link";
import {
  Activity,
  ArrowRight,
  Bot,
  History,
  Database,
  Gauge,
  Lightbulb,
  Plus,
  ShieldCheck,
  Sparkles,
  Upload,
  Workflow,
} from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { PageHeader } from "@/components/ui/page-header";
import { StatCard } from "@/components/ui/stat-card";
import { ChartCard } from "@/components/ui/chart-card";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Progress, Skeleton } from "@/components/ui/feedback";
import { Badge } from "@/components/ui/badge";
import { FeatureStatusBadge, POLICY_LABELS } from "@/components/ui/status";
import { ActivityList } from "@/components/activity/activity-list";
import { RowsByDatasetChart, RunsDonut } from "@/components/dashboard/charts";
import { useActivity, useAiStatus, useAiUsage, useDataStats, useInsights } from "@/lib/queries/org";
import { useQueryHistory } from "@/lib/queries/bi";
import { datasetGrowth, dailyRequests, pctChange } from "@/lib/dashboard";
import { SourceBadge } from "@/components/ask/sql-code";
import { VerdictBadge } from "@/components/ask/query-history";
import { useDatasets } from "@/lib/queries/datasets";
import { isUnavailableEndpoint } from "@/lib/errors";
import { formatBytes, formatNumber, formatRelative, toPercentScore } from "@/lib/utils";
import { scoreTone } from "@/components/ui/score-ring";
import { aiHealth } from "@/components/shell/ai-status-pill";
import { HomeDashboardSection } from "@/components/dashboards/dashboard-view";
import { useDashboards } from "@/lib/queries/dashboards";
import { pickHomeDashboard } from "@/lib/dashboards/home";

function greeting(now = new Date()) {
  const h = now.getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

function Unavailable({ what }: { what: string }) {
  return (
    <EmptyState
      compact
      icon={<Activity />}
      title={`${what} isn't available yet`}
      description="This panel fills in automatically once the service responds."
      className="h-full"
    />
  );
}

export default function DashboardPage() {
  const { slug, me, org, summary, can } = useOrgContext();
  const stats = useDataStats(slug);
  const ai = useAiStatus(slug);
  const datasets = useDatasets(slug);
  const activity = useActivity(slug);
  const insights = useInsights(slug);
  const canUsage = can("org.settings.edit") || can("audit.view");
  const usage = useAiUsage(slug, 14, canUsage);
  const history = useQueryHistory(slug, { limit: 6 }, can("bi.chat"));
  const dashboards = useDashboards(slug, can("dashboards.view"));
  const homeDashboard = pickHomeDashboard(dashboards.data, me.user.id);
  const [hello, setHello] = React.useState("Welcome");
  React.useEffect(() => setHello(greeting()), []);

  const firstName = (me.user.full_name || "").split(" ")[0];
  const s = stats.data;
  const avgQuality = toPercentScore(s?.avg_quality);
  const budget = ai.data?.org_budget;
  const health = aiHealth(ai.data);

  // KPI deltas come only from real data (previous-period stats when the API sends them, dataset creation dates,
  // daily AI usage). Nothing is shown when there is nothing to compare against.
  const growth = React.useMemo(() => datasetGrowth(datasets.data ?? []), [datasets.data]);
  const datasetsDelta = s?.previous?.datasets !== undefined ? pctChange(s.datasets, s.previous.datasets) : growth.pct;
  const qualityDelta = s?.previous ? pctChange(toPercentScore(s.avg_quality), toPercentScore(s.previous.avg_quality)) : null;
  const pipelinesDelta = s?.previous ? pctChange(s.pipelines_active, s.previous.pipelines_active) : null;
  const aiDaily = React.useMemo(() => dailyRequests(usage.data), [usage.data]);
  const aiDelta = aiDaily.length >= 2 ? pctChange(aiDaily[aiDaily.length - 1].requests, aiDaily[aiDaily.length - 2].requests) : null;

  const rowsByDataset = React.useMemo(
    () =>
      (datasets.data ?? [])
        .filter((d) => (d.row_count ?? 0) > 0)
        .sort((a, b) => (b.row_count ?? 0) - (a.row_count ?? 0))
        .slice(0, 8)
        .map((d) => ({ name: d.name, rows: d.row_count ?? 0 })),
    [datasets.data],
  );

  const statHint = (fallback: React.ReactNode) =>
    stats.isError ? (isUnavailableEndpoint(stats.error) ? "Data service not deployed yet" : "Couldn't load") : fallback;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={org?.name ?? summary?.name}
        title={firstName ? `${hello}, ${firstName}` : hello}
        description="Your organization at a glance: data, pipelines, quality and AI usage."
        actions={
          <>
            {can("bi.chat") && (
              <Button variant="secondary" asChild>
                <Link href={`/o/${slug}/ask`}>
                  <Sparkles /> Ask AI
                </Link>
              </Button>
            )}
            {can("datasets.create") && (
              <Button asChild>
                <Link href={`/o/${slug}/data/datasets?upload=1`}>
                  <Upload /> Upload data
                </Link>
              </Button>
            )}
          </>
        }
      />

      {/* KPI row */}
      <section aria-label="Key metrics" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Total datasets"
          icon={<Database />}
          loading={stats.isLoading}
          value={s ? formatNumber(s.datasets) : "—"}
          hint={statHint(s ? `${formatNumber(s.rows_total, { compact: true })} rows · ${formatBytes(s.storage_bytes)}` : null)}
          delta={datasetsDelta !== null ? { value: datasetsDelta, label: s?.previous ? "vs last period" : "last 7 days" } : null}
          href={`/o/${slug}/data/datasets`}
        />
        <StatCard
          label="Active pipelines"
          icon={<Workflow />}
          accent="teal"
          loading={stats.isLoading}
          value={s ? formatNumber(s.pipelines_active) : "—"}
          delta={pipelinesDelta !== null ? { value: pipelinesDelta, label: "vs last period" } : null}
          href={`/o/${slug}/data/pipelines`}
          hint={statHint(
            s ? `${formatNumber(s.runs_7d.succeeded + s.runs_7d.failed)} runs in the last 7 days` : null,
          )}
        />
        <StatCard
          label="Avg data quality"
          icon={<Gauge />}
          accent={avgQuality === null ? "accent" : scoreTone(avgQuality) === "success" ? "success" : "warning"}
          loading={stats.isLoading}
          value={avgQuality === null ? "—" : `${Math.round(avgQuality)}`}
          delta={qualityDelta !== null ? { value: qualityDelta, label: "vs last period" } : null}
          href={`/o/${slug}/data/quality`}
          hint={statHint(avgQuality === null ? "No profiled datasets yet" : "Score out of 100 across profiled datasets")}
        />
        <StatCard
          label="AI usage today"
          icon={<Bot />}
          loading={ai.isLoading}
          delta={aiDelta !== null ? { value: aiDelta, label: "vs yesterday", positiveIsGood: false } : null}
          sparkline={aiDaily.length > 1 ? aiDaily.map((d) => d.requests) : null}
          value={
            budget ? (
              <>
                {formatNumber(budget.used)}
                <span className="text-base font-medium text-fg-subtle"> / {budget.limit > 0 ? formatNumber(budget.limit) : "∞"}</span>
              </>
            ) : (
              "—"
            )
          }
          hint={
            ai.isError
              ? isUnavailableEndpoint(ai.error)
                ? "AI service not deployed yet"
                : "Couldn't load"
              : ai.data
                ? `${POLICY_LABELS[ai.data.policy] ?? ai.data.policy} · ${health.label}`
                : null
          }
          footer={
            budget && budget.limit > 0 ? (
              <Progress
                value={budget.used}
                max={budget.limit}
                tone={health.tone === "danger" ? "danger" : health.tone === "warning" ? "warning" : "accent"}
                label="AI budget used"
              />
            ) : undefined
          }
        />
      </section>

      {/* The caller's home dashboard (is_default), from /dashboards. */}
      {homeDashboard && <HomeDashboardSection dashboardId={homeDashboard.id} />}

      {/* Charts */}
      <section aria-label="Charts" className="grid gap-4 lg:grid-cols-12">
        <ChartCard
          className="lg:col-span-7"
          title="Data processed"
          description="Rows ingested per dataset (largest first)"
          loading={datasets.isLoading}
          height={260}
          error={datasets.isError ? <ErrorState compact error={datasets.error} onRetry={() => void datasets.refetch()} /> : undefined}
          empty={
            rowsByDataset.length === 0 ? (
              <EmptyState
                compact
                icon={<Database />}
                title="No data processed yet"
                description="Upload a CSV or Excel file to see ingestion volume here."
                action={
                  can("datasets.create") ? (
                    <Button size="sm" asChild>
                      <Link href={`/o/${slug}/data/datasets?upload=1`}>
                        <Upload /> Upload dataset
                      </Link>
                    </Button>
                  ) : undefined
                }
              />
            ) : undefined
          }
          table={{
            columns: [
              { key: "name", label: "Dataset" },
              { key: "rows", label: "Rows", align: "right", format: (v) => formatNumber(Number(v)) },
            ],
            rows: rowsByDataset,
          }}
        >
          <RowsByDatasetChart data={rowsByDataset} />
        </ChartCard>
        <ChartCard
          className="lg:col-span-5"
          title="Pipeline runs"
          description="Outcomes over the last 7 days"
          loading={stats.isLoading}
          height={260}
          error={
            stats.isError ? (
              isUnavailableEndpoint(stats.error) ? (
                <Unavailable what="Pipeline status" />
              ) : (
                <ErrorState compact error={stats.error} onRetry={() => void stats.refetch()} />
              )
            ) : undefined
          }
          empty={
            s && s.runs_7d.succeeded + s.runs_7d.failed === 0 ? (
              <EmptyState
                compact
                icon={<Workflow />}
                title="No pipeline runs this week"
                description="Run a pipeline to track its success rate here."
                className="h-full"
              />
            ) : undefined
          }
          table={
            s
              ? {
                  columns: [
                    { key: "outcome", label: "Outcome" },
                    { key: "count", label: "Runs", align: "right", format: (v) => formatNumber(Number(v)) },
                  ],
                  rows: [
                    { outcome: "Succeeded", count: s.runs_7d.succeeded },
                    { outcome: "Failed", count: s.runs_7d.failed },
                  ],
                }
              : undefined
          }
        >
          {s && <RunsDonut succeeded={s.runs_7d.succeeded} failed={s.runs_7d.failed} />}
        </ChartCard>
      </section>

      {/* Activity + AI */}
      <section className="grid gap-4 lg:grid-cols-12">
        <Card className="lg:col-span-7">
          <CardHeader
            icon={<Activity />}
            title="Recent activity"
            description="Changes across your organization"
            actions={
              can("audit.view") ? (
                <Button variant="ghost" size="sm" asChild>
                  <Link href={`/o/${slug}/audit`}>
                    Audit log <ArrowRight />
                  </Link>
                </Button>
              ) : undefined
            }
          />
          <CardContent className="pb-2">
            {activity.isLoading ? (
              <div className="space-y-4 py-2">
                {[0, 1, 2, 3].map((i) => (
                  <div key={i} className="flex gap-3">
                    <Skeleton className="size-8 rounded-full" />
                    <div className="flex-1 space-y-1.5">
                      <Skeleton className="h-3.5 w-3/4" />
                      <Skeleton className="h-3 w-24" />
                    </div>
                  </div>
                ))}
              </div>
            ) : activity.isError ? (
              isUnavailableEndpoint(activity.error) ? (
                <Unavailable what="The activity feed" />
              ) : (
                <ErrorState compact error={activity.error} onRetry={() => void activity.refetch()} />
              )
            ) : (activity.data ?? []).length === 0 ? (
              <EmptyState compact icon={<Activity />} title="No activity yet" description="Uploads, invitations and policy changes will appear here." />
            ) : (
              <ActivityList events={activity.data!} max={8} />
            )}
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4 lg:col-span-5">
          <AiInsightsPanel
            loading={insights.isLoading}
            error={insights.isError ? insights.error : null}
            onRetry={() => void insights.refetch()}
            insights={insights.data ?? []}
            policy={ai.data?.policy ?? org?.ai_data_policy}
            slug={slug}
          />
          {can("bi.chat") && <RecentQuestions slug={slug} q={history} />}
          <Card>
            <CardHeader title="Quick actions" />
            <CardContent className="grid gap-2 sm:grid-cols-3 lg:grid-cols-1">
              <QuickAction
                href={`/o/${slug}/data/datasets?upload=1`}
                icon={<Upload />}
                label="Upload data"
                disabled={!can("datasets.create")}
              />
              <QuickAction href={`/o/${slug}/ask`} icon={<Sparkles />} label="Ask AI" disabled={!can("bi.chat")} />
              <QuickAction
                href={`/o/${slug}/data/pipelines?new=1`}
                icon={<Plus />}
                label="New pipeline"
                disabled={!can("pipelines.manage")}
              />
            </CardContent>
          </Card>
        </div>
      </section>
    </div>
  );
}

function QuickAction({
  href,
  icon,
  label,
  planned,
  disabled,
}: {
  href: string;
  icon: React.ReactNode;
  label: string;
  planned?: boolean;
  disabled?: boolean;
}) {
  if (disabled) return null;
  return (
    <Link
      href={href}
      className="group flex items-center gap-2.5 rounded-control border border-border bg-surface px-3 py-2.5 text-sm font-medium text-fg transition-[border-color,background-color] duration-150 hover:border-border-strong hover:bg-surface-2 focus-ring"
    >
      <span className="flex size-7 items-center justify-center rounded-control bg-accent-soft text-accent-soft-fg [&_svg]:size-4">{icon}</span>
      <span className="truncate">{label}</span>
      {planned ? (
        <FeatureStatusBadge status="planned" className="ml-auto" />
      ) : (
        <ArrowRight className="ml-auto size-4 text-fg-subtle transition-transform group-hover:translate-x-0.5" aria-hidden />
      )}
    </Link>
  );
}

function AiInsightsPanel({
  loading,
  error,
  onRetry,
  insights,
  policy,
  slug,
}: {
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  insights: Array<{ kind: string; title: string; text: string; dataset_id: string | null }>;
  policy: string | undefined;
  slug: string;
}) {
  return (
    <Card className="relative overflow-hidden">
      <div
        className="pointer-events-none absolute inset-x-0 top-0 h-24 opacity-70"
        style={{ background: "linear-gradient(180deg, var(--accent-soft) 0%, transparent 100%)" }}
        aria-hidden
      />
      <CardHeader
        className="relative"
        icon={<Lightbulb />}
        title="AI insights"
        description="Locally computed and Gemini-assisted findings"
        actions={
          policy ? (
            <Badge tone="teal" title="Organization AI data policy">
              <ShieldCheck /> {POLICY_LABELS[policy] ?? policy}
            </Badge>
          ) : undefined
        }
      />
      <CardContent className="relative">
        {loading ? (
          <div className="space-y-3">
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-14 w-full" />
          </div>
        ) : error ? (
          isUnavailableEndpoint(error) ? (
            <EmptyState
              compact
              icon={<Lightbulb />}
              title="Insights aren't available yet"
              description="Insights appear once the AI service is deployed and datasets are profiled."
            />
          ) : (
            <ErrorState compact error={error} onRetry={onRetry} />
          )
        ) : insights.length === 0 ? (
          <EmptyState
            compact
            icon={<Lightbulb />}
            title="No insights yet"
            description={
              policy === "synthetic_only"
                ? "Under the synthetic_only policy, AI insights are only generated from datasets classified synthetic or public."
                : "Insights appear as datasets are profiled and analyzed."
            }
            action={
              <Button variant="secondary" size="sm" asChild>
                <Link href={`/o/${slug}/settings/ai-policy`}>
                  <ShieldCheck /> Review AI policy
                </Link>
              </Button>
            }
          />
        ) : (
          <ul className="space-y-2.5">
            {insights.slice(0, 5).map((ins, i) => (
              <li key={i} className="rounded-control border border-border bg-surface px-3.5 py-3">
                <div className="flex items-center gap-2">
                  <Badge tone="accent">{ins.kind.replace(/_/g, " ")}</Badge>
                  <span className="truncate text-sm font-medium text-fg">{ins.title}</span>
                </div>
                <p className="mt-1.5 text-sm text-fg-muted">{ins.text}</p>
                {ins.dataset_id && (
                  <span className="mt-1.5 flex flex-wrap gap-3">
                    <Link
                      href={`/o/${slug}/data/datasets/${ins.dataset_id}`}
                      className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:underline"
                    >
                      Open dataset <ArrowRight className="size-3" />
                    </Link>
                    <Link
                      href={`/o/${slug}/ask?dataset=${ins.dataset_id}`}
                      className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:underline"
                    >
                      Ask about it <Sparkles className="size-3" />
                    </Link>
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function RecentQuestions({ slug, q }: { slug: string; q: ReturnType<typeof useQueryHistory> }) {
  return (
    <Card>
      <CardHeader
        icon={<History />}
        title="Recent questions"
        description="From the guarded query audit log"
        actions={
          <Button variant="ghost" size="sm" asChild>
            <Link href={`/o/${slug}/ask/history`}>
              All <ArrowRight />
            </Link>
          </Button>
        }
      />
      <CardContent>
        {q.isLoading ? (
          <div className="space-y-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : q.isError ? (
          isUnavailableEndpoint(q.error) ? (
            <Unavailable what="Query history" />
          ) : (
            <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
          )
        ) : (q.data ?? []).length === 0 ? (
          <EmptyState
            compact
            icon={<Sparkles />}
            title="No questions yet"
            description="Ask your data a question to see it here."
            action={
              <Button size="sm" variant="secondary" asChild>
                <Link href={`/o/${slug}/ask`}>Ask AI</Link>
              </Button>
            }
          />
        ) : (
          <ul className="divide-y divide-border">
            {(q.data ?? []).slice(0, 5).map((h) => (
              <li key={h.id} className="flex items-center gap-2 py-2">
                <span className="min-w-0 flex-1 truncate text-sm text-fg" title={h.question ?? h.executed_sql ?? ""}>
                  {h.question ?? <span className="font-mono text-xs text-fg-muted">{h.executed_sql}</span>}
                </span>
                <SourceBadge source={h.source} />
                {h.verdict !== "allowed" && <VerdictBadge verdict={h.verdict} />}
                <span className="shrink-0 text-xs text-fg-subtle">{formatRelative(h.occurred_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
