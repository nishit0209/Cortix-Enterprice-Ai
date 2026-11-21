"use client";

import * as React from "react";
import { Activity, Bot, CalendarRange, Database, FileText, HardDrive, Sparkles, Users, Workflow } from "lucide-react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { CHART_COLORS, ChartCard, ChartTooltip, chartTheme } from "@/components/ui/chart-card";
import { EmptyState, ErrorState } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { StatCard } from "@/components/ui/stat-card";
import { FeatureStatusBadge } from "@/components/ui/status";
import { isUnavailableEndpoint } from "@/lib/errors";
import { useAiUsage } from "@/lib/queries/org";
import { useOrgUsage } from "@/lib/queries/usage";
import {
  USAGE_METRICS,
  aiByModel,
  aiLookbackDays,
  breakdown,
  halfOverHalf,
  isUnavailable,
  mergeDaily,
  metricOf,
  presetRange,
  rangeDays,
  validateRange,
  type DateRange,
  type UsageMetric,
} from "@/lib/usage";
import { formatBytes, formatDate, formatDateTime, formatNumber, titleCase } from "@/lib/utils";

const PRESETS = [7, 30, 90, 365] as const;
const METRIC_LABELS: Record<UsageMetric, string> = {
  queries: "Queries",
  active_users: "Active users",
  datasets: "Datasets",
  storage: "Storage",
  ai: "AI",
  pipeline_runs: "Pipeline runs",
  documents: "Documents",
};

const shortDay = (d: unknown) => formatDate(String(d)).replace(/,? \d{4}$/, "");
const pct = (v: number | null) => (v === null ? null : { value: v * 100, label: "2nd half vs 1st" });

export function UsageView() {
  const { slug, can } = useOrgContext();
  const [preset, setPreset] = React.useState<number | "custom">(30);
  const [custom, setCustom] = React.useState<DateRange>(() => presetRange(30));
  const range = preset === "custom" ? custom : presetRange(preset);
  const rangeError = validateRange(range);
  const [metric, setMetric] = React.useState<UsageMetric | "all">("all");
  const q = useOrgUsage(slug, range, metric === "all" ? null : metric, !rangeError);
  const canAi = can("org.settings.edit") || can("audit.view");
  const showAi = metric === "all" || metric === "ai";
  const aiQ = useAiUsage(slug, aiLookbackDays(range), canAi && showAi && !rangeError);
  const r = q.data;
  const show = (m: UsageMetric) => metric === "all" || metric === m;

  if (q.isError && isUnavailableEndpoint(q.error)) {
    return (
      <div className="space-y-6">
        <PageHeader title="Usage" />
        <Card>
          <EmptyState icon={<Activity />} title="Usage isn't available yet" description="This server doesn't report org usage yet. Nothing is simulated." />
        </Card>
      </div>
    );
  }

  const queries = metricOf(r, "queries");
  const active = metricOf(r, "active_users");
  const datasets = metricOf(r, "datasets");
  const storage = metricOf(r, "storage");
  const ai = metricOf(r, "ai");
  const runs = metricOf(r, "pipeline_runs");
  const docs = metricOf(r, "documents");
  const loading = q.isLoading;
  const models = aiByModel(aiQ.data, range);
  const features = (aiQ.data?.by_feature ?? []).filter((f) => f.requests > 0);

  const activity = mergeDaily({ queries: queries?.per_day, active_users: active?.per_day, pipeline_runs: runs?.per_day });
  const created = mergeDaily({ datasets: datasets?.per_day, documents: docs?.per_day });
  const aiDaily = (ai?.per_day ?? []).map((d) => ({ day: d.day, requests: d.requests, tokens: d.tokens }));

  return (
    <div className="space-y-6">
      <PageHeader
        title="Usage"
        badge={<FeatureStatusBadge status="working" />}
        description="Queries, active members, data, storage, AI and pipeline activity for this organization, aggregated from the database. Days are UTC."
      />

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 py-4">
          <div className="flex flex-wrap gap-1" role="group" aria-label="Date range">
            {PRESETS.map((d) => (
              <Button key={d} size="sm" variant={preset === d ? "primary" : "secondary"} aria-pressed={preset === d} onClick={() => setPreset(d)}>
                {d === 365 ? "12 months" : `${d} days`}
              </Button>
            ))}
            <Button
              size="sm"
              variant={preset === "custom" ? "primary" : "secondary"}
              aria-pressed={preset === "custom"}
              onClick={() => {
                setCustom(range);
                setPreset("custom");
              }}
            >
              <CalendarRange /> Custom
            </Button>
          </div>
          {preset === "custom" && (
            <div className="flex items-center gap-2">
              <Input type="date" aria-label="From" className="w-40" value={custom.from} max={custom.to} onChange={(e) => setCustom((c) => ({ ...c, from: e.target.value }))} />
              <span className="text-fg-subtle">to</span>
              <Input type="date" aria-label="To" className="w-40" value={custom.to} min={custom.from} onChange={(e) => setCustom((c) => ({ ...c, to: e.target.value }))} />
            </div>
          )}
          <Select value={metric} onValueChange={(v) => setMetric(v as UsageMetric | "all")}>
            <SelectTrigger className="w-44" aria-label="Metric">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All metrics</SelectItem>
              {USAGE_METRICS.map((m) => (
                <SelectItem key={m} value={m}>
                  {METRIC_LABELS[m]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="ml-auto text-xs text-fg-muted">
            {rangeError ? (
              <span className="text-danger">{rangeError}</span>
            ) : (
              <>
                {formatDate(range.from)} – {formatDate(range.to)} · {formatNumber(rangeDays(range))} days
                {r?.generated_at && <> · generated {formatDateTime(r.generated_at)}</>}
              </>
            )}
          </div>
        </CardContent>
      </Card>

      {q.isError && <ErrorState error={q.error} onRetry={() => void q.refetch()} />}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {show("queries") && (
          <StatCard label="Queries" icon={<Activity />} loading={loading} value={queries ? formatNumber(queries.total) : "—"} hint={unavailableHint(r, "queries")} sparkline={queries?.per_day.map((p) => p.count)} delta={pct(halfOverHalf(queries?.per_day))} />
        )}
        {show("active_users") && (
          <StatCard
            label="Active members"
            icon={<Users />}
            loading={loading}
            value={active ? formatNumber(active.distinct) : "—"}
            hint={active ? `of ${formatNumber(active.members_active)} active members` : unavailableHint(r, "active_users")}
            sparkline={active?.per_day.map((p) => p.count)}
            accent="teal"
          />
        )}
        {show("ai") && (
          <StatCard
            label="AI requests"
            icon={<Sparkles />}
            loading={loading}
            value={ai ? formatNumber(ai.requests) : "—"}
            hint={ai ? `${formatNumber(ai.tokens, { compact: true })} tokens · ${formatNumber(ai.blocked)} blocked` : unavailableHint(r, "ai")}
            sparkline={ai?.per_day.map((p) => p.requests)}
          />
        )}
        {show("storage") && (
          <StatCard label="Storage" icon={<HardDrive />} loading={loading} value={storage ? formatBytes(storage.total_bytes) : "—"} hint={storage ? `as of ${formatDateTime(storage.as_of)}` : unavailableHint(r, "storage")} accent="warning" />
        )}
        {show("datasets") && (
          <StatCard label="Datasets" icon={<Database />} loading={loading} value={datasets ? formatNumber(datasets.total) : "—"} hint={datasets ? `${formatNumber(datasets.created)} created in range` : unavailableHint(r, "datasets")} />
        )}
        {show("pipeline_runs") && (
          <StatCard
            label="Pipeline runs"
            icon={<Workflow />}
            loading={loading}
            value={runs ? formatNumber(runs.total) : "—"}
            hint={runs ? `${formatNumber(runs.by_status.failed ?? 0)} failed` : unavailableHint(r, "pipeline_runs")}
            sparkline={runs?.per_day.map((p) => p.count)}
            accent="success"
          />
        )}
        {show("documents") && (
          <StatCard label="Documents" icon={<FileText />} loading={loading} value={docs ? formatNumber(docs.total) : "—"} hint={docs ? `${formatNumber(docs.created)} added in range` : unavailableHint(r, "documents")} />
        )}
      </div>

      {(show("queries") || show("active_users") || show("pipeline_runs")) && (
        <ChartCard
          title="Activity per day"
          description="Governed queries, distinct active members and pipeline runs."
          loading={loading}
          height={260}
          empty={!loading && activity.every((d) => !Number(d.queries) && !Number(d.active_users) && !Number(d.pipeline_runs)) ? <EmptyState compact icon={<Activity />} title="No activity in this range" /> : undefined}
          table={{
            columns: [
              { key: "day", label: "Day" },
              ...(["queries", "active_users", "pipeline_runs"] as const).filter(show).map((k) => ({ key: k, label: METRIC_LABELS[k], align: "right" as const, format: (v: unknown) => formatNumber(Number(v)) })),
            ],
            rows: activity,
          }}
        >
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={activity} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
              <CartesianGrid {...chartTheme.grid} />
              <XAxis dataKey="day" {...chartTheme.axis} tickFormatter={shortDay} minTickGap={24} />
              <YAxis {...chartTheme.axis} allowDecimals={false} tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
              <Tooltip content={<ChartTooltip formatter={(v) => formatNumber(Number(v))} />} labelFormatter={(l) => formatDate(String(l))} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              {(["queries", "active_users", "pipeline_runs"] as const).filter(show).map((k, i) => (
                <Area key={k} type="monotone" dataKey={k} name={METRIC_LABELS[k]} stroke={CHART_COLORS[i]} fill={CHART_COLORS[i]} fillOpacity={0.12} strokeWidth={2} animationDuration={500} />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        </ChartCard>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        {show("ai") && (
          <ChartCard
            title="AI requests and tokens"
            description={ai?.timezone ? `Gemini usage per day (counters kept in ${ai.timezone}).` : "Gemini usage per day."}
            loading={loading}
            empty={!loading && !aiDaily.some((d) => d.requests) ? <EmptyState compact icon={<Bot />} title={ai ? "No AI requests in this range" : "AI usage isn't available"} /> : undefined}
            table={{
              columns: [
                { key: "day", label: "Day" },
                { key: "requests", label: "Requests", align: "right", format: (v) => formatNumber(Number(v)) },
                { key: "tokens", label: "Tokens", align: "right", format: (v) => formatNumber(Number(v)) },
              ],
              rows: aiDaily,
            }}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={aiDaily} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                <CartesianGrid {...chartTheme.grid} />
                <XAxis dataKey="day" {...chartTheme.axis} tickFormatter={shortDay} minTickGap={24} />
                <YAxis yAxisId="r" {...chartTheme.axis} allowDecimals={false} tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
                <YAxis yAxisId="t" orientation="right" {...chartTheme.axis} tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
                <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => formatNumber(Number(v))} />} labelFormatter={(l) => formatDate(String(l))} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar yAxisId="r" dataKey="requests" name="Requests" fill={CHART_COLORS[0]} radius={[3, 3, 0, 0]} maxBarSize={18} animationDuration={500} />
                <Bar yAxisId="t" dataKey="tokens" name="Tokens" fill={CHART_COLORS[2]} fillOpacity={0.7} radius={[3, 3, 0, 0]} maxBarSize={18} animationDuration={500} />
              </BarChart>
            </ResponsiveContainer>
          </ChartCard>
        )}
        {(show("datasets") || show("documents")) && (
          <ChartCard
            title="New datasets and documents"
            loading={loading}
            empty={!loading && !created.some((d) => Number(d.datasets) || Number(d.documents)) ? <EmptyState compact icon={<Database />} title="Nothing added in this range" /> : undefined}
            table={{
              columns: [
                { key: "day", label: "Day" },
                { key: "datasets", label: "Datasets", align: "right" },
                { key: "documents", label: "Documents", align: "right" },
              ],
              rows: created,
            }}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={created} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                <CartesianGrid {...chartTheme.grid} />
                <XAxis dataKey="day" {...chartTheme.axis} tickFormatter={shortDay} minTickGap={24} />
                <YAxis {...chartTheme.axis} allowDecimals={false} />
                <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => formatNumber(Number(v))} />} labelFormatter={(l) => formatDate(String(l))} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                {show("datasets") && <Bar dataKey="datasets" name="Datasets" stackId="c" fill={CHART_COLORS[1]} maxBarSize={18} animationDuration={500} />}
                {show("documents") && <Bar dataKey="documents" name="Documents" stackId="c" fill={CHART_COLORS[3]} radius={[3, 3, 0, 0]} maxBarSize={18} animationDuration={500} />}
              </BarChart>
            </ResponsiveContainer>
          </ChartCard>
        )}
      </div>

      <div className="grid gap-5 lg:grid-cols-3">
        {show("queries") && queries && <BreakdownCard title="Queries by verdict" rows={breakdown(queries.by_verdict)} />}
        {show("queries") && queries && <BreakdownCard title="Queries by purpose" rows={breakdown(queries.by_purpose)} />}
        {show("pipeline_runs") && runs && <BreakdownCard title="Pipeline runs by status" rows={breakdown(runs.by_status)} />}
        {show("datasets") && datasets && <BreakdownCard title="Datasets by status" rows={breakdown(datasets.by_status)} />}
        {show("documents") && docs && <BreakdownCard title="Documents by status" rows={breakdown(docs.by_status)} />}
        {show("storage") && storage && (
          <BreakdownCard
            title="Storage"
            format={formatBytes}
            rows={breakdown({ Warehouse: storage.warehouse_bytes, Uploads: storage.uploads_bytes, Documents: storage.documents_bytes })}
          />
        )}
      </div>

      {show("queries") && queries?.per_user && (
        <Card>
          <CardHeader icon={<Users />} title="Top members by queries" />
          <CardContent>
            {queries.per_user.length === 0 ? (
              <p className="text-sm text-fg-muted">No member ran a query in this range.</p>
            ) : (
              <BarList rows={queries.per_user.map((u) => ({ name: u.email ?? u.user_id.slice(0, 8), value: u.count }))} />
            )}
          </CardContent>
        </Card>
      )}

      {showAi && canAi && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card>
            <CardHeader icon={<Bot />} title="AI by model" description="Requests and tokens per Gemini model in the selected range." />
            <CardContent>
              {aiQ.isError ? (
                <ErrorState compact error={aiQ.error} onRetry={() => void aiQ.refetch()} />
              ) : models.length === 0 ? (
                <p className="text-sm text-fg-muted">{aiQ.isLoading ? "Loading…" : "No AI requests in this range."}</p>
              ) : (
                <table className="w-full text-sm">
                  <caption className="sr-only">AI usage by model</caption>
                  <thead className="text-xs text-fg-muted">
                    <tr className="border-b border-border">
                      <th className="py-2 text-left font-medium">Model</th>
                      <th className="py-2 text-right font-medium">Requests</th>
                      <th className="py-2 text-right font-medium">Tokens</th>
                      <th className="py-2 text-right font-medium">Blocked</th>
                      <th className="py-2 text-right font-medium">Errors</th>
                    </tr>
                  </thead>
                  <tbody>
                    {models.map((m) => (
                      <tr key={m.model} className="border-b border-border/60 last:border-0">
                        <td className="py-1.5 font-mono text-xs">{m.model}</td>
                        <td className="tabular py-1.5 text-right">{formatNumber(m.requests)}</td>
                        <td className="tabular py-1.5 text-right">{formatNumber(m.tokens)}</td>
                        <td className="tabular py-1.5 text-right">{formatNumber(m.blocked)}</td>
                        <td className="tabular py-1.5 text-right">{formatNumber(m.errors)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader
              icon={<Sparkles />}
              title="AI by feature"
              description={aiQ.data ? `Last ${formatNumber(aiQ.data.range.days)} days (${aiQ.data.range.timezone}); the AI log keeps at most 90 days.` : undefined}
            />
            <CardContent>
              {features.length === 0 ? (
                <p className="text-sm text-fg-muted">{aiQ.isLoading ? "Loading…" : "No AI requests recorded."}</p>
              ) : (
                <BarList rows={features.map((f) => ({ name: titleCase(f.feature.replace(/[._]/g, " ")), value: f.requests, hint: `${formatNumber(f.input_tokens + f.output_tokens, { compact: true })} tokens` }))} />
              )}
              {aiQ.data?.by_status && Object.keys(aiQ.data.by_status).length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {breakdown(aiQ.data.by_status).map((s) => (
                    <Badge key={s.name} tone={s.name === "ok" || s.name === "success" ? "success" : s.name.includes("block") ? "warning" : s.name.includes("error") ? "danger" : "neutral"}>
                      {s.name} · {formatNumber(s.value)}
                    </Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

function unavailableHint(r: Parameters<typeof isUnavailable>[0], key: UsageMetric): string | undefined {
  return isUnavailable(r, key) ? "Not available on this server" : undefined;
}

function BreakdownCard({ title, rows, format = (v: number) => formatNumber(v) }: { title: string; rows: Array<{ name: string; value: number }>; format?: (v: number) => string }) {
  return (
    <Card>
      <CardHeader title={title} />
      <CardContent>{rows.length === 0 ? <p className="text-sm text-fg-muted">Nothing in this range.</p> : <BarList rows={rows.map((r) => ({ ...r, name: titleCase(r.name.replace(/_/g, " ")) }))} format={format} />}</CardContent>
    </Card>
  );
}

/** Horizontal proportional bars (accessible list; the numbers are the content, the bar is decoration). */
function BarList({ rows, format = (v: number) => formatNumber(v) }: { rows: Array<{ name: string; value: number; hint?: string }>; format?: (v: number) => string }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="space-y-2">
      {rows.map((r, i) => (
        <li key={r.name} className="space-y-1">
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="truncate text-fg">{r.name}</span>
            <span className="tabular text-fg-muted">
              {format(r.value)}
              {r.hint && <span className="ml-1.5 text-xs text-fg-subtle">{r.hint}</span>}
            </span>
          </div>
          <div className="h-1.5 rounded-full bg-surface-2" aria-hidden>
            <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${(r.value / max) * 100}%`, background: CHART_COLORS[i % CHART_COLORS.length] }} />
          </div>
        </li>
      ))}
    </ul>
  );
}
