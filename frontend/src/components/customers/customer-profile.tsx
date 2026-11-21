"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, CalendarClock, Gauge, GitMerge, Layers, Receipt, ShoppingBag, UserRound } from "lucide-react";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { Pager } from "@/components/ui/pager";
import { StatCard } from "@/components/ui/stat-card";
import { StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { displayValue, explanationOf } from "@/lib/mdm";
import { useCustomer, useCustomerScores, useCustomerTimeline, type CustomerScore } from "@/lib/queries/customers";
import { cn, formatDateTime, formatNumber, formatPercent, formatRelative, titleCase } from "@/lib/utils";
import { ModuleError, NativeSelect, ScoreBar } from "@/components/mdm/common";

const SCORE_LABELS: Record<string, string> = { churn: "Churn risk", clv: "Lifetime value", nba: "Next best action" };

export function scoreDisplay(s: Pick<CustomerScore, "score_type" | "value">): string {
  const v = s.value;
  if (typeof v === "number") return s.score_type === "churn" && v >= 0 && v <= 1 ? formatPercent(v, 0) : formatNumber(v);
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    const inner = o.probability ?? o.value ?? o.prediction ?? o.action ?? o.label;
    if (inner !== undefined) return scoreDisplay({ score_type: s.score_type, value: inner });
  }
  return displayValue(v) || "—";
}

export function CustomerProfileView() {
  const { id } = useParams<{ id: string }>();
  const { slug } = useOrgContext();
  const q = useCustomer(slug, id);
  const p = q.data;
  useSetBreadcrumb(p?.display_name);

  if (q.isError) {
    return (
      <Card>
        <ModuleError error={q.error} what="This customer" onRetry={() => void q.refetch()} />
        <div className="flex justify-center pb-6">
          <Button variant="secondary" asChild>
            <Link href={`/o/${slug}/customers`}>
              <ArrowLeft /> Customers
            </Link>
          </Button>
        </div>
      </Card>
    );
  }
  if (!p) return <Skeleton className="h-96 rounded-card" />;
  const golden = Object.entries(p.golden ?? {}).filter(([, v]) => v !== null && v !== "");

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/customers`} className="inline-flex items-center gap-1 hover:underline">
            <ArrowLeft className="size-3" /> Customer 360
          </Link>
        }
        title={p.display_name || "Unnamed customer"}
        description={[p.email, p.phone].filter(Boolean).join(" · ") || undefined}
        badge={
          <span className="flex flex-wrap gap-1">
            {p.sources.map((s) => (
              <Badge key={s} tone="outline">
                {s}
              </Badge>
            ))}
          </span>
        }
      />
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Customer summary">
        <StatCard label="Orders" icon={<ShoppingBag />} value={formatNumber(p.order_count)} hint={p.last_order_at ? `Last ${formatRelative(p.last_order_at)}` : "No orders"} />
        <StatCard label="Total spend" icon={<Receipt />} accent="success" value={formatNumber(p.monetary)} />
        <StatCard label="Events" icon={<CalendarClock />} accent="teal" value={formatNumber(p.event_count)} hint={p.first_seen ? `Since ${formatDateTime(p.first_seen)}` : undefined} />
        <StatCard label="Linked records" icon={<GitMerge />} accent="accent" value={formatNumber(p.record_count)} />
      </section>

      <Tabs defaultValue="overview">
        <TabsList aria-label="Customer sections">
          <TabsTrigger value="overview">
            <UserRound /> Profile
          </TabsTrigger>
          <TabsTrigger value="timeline">
            <CalendarClock /> Timeline
          </TabsTrigger>
          <TabsTrigger value="scores">
            <Gauge /> Scores
          </TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader title="Golden record" description="Each value is chosen by the survivorship rule shown next to it." />
              <CardContent>
                {golden.length === 0 ? (
                  <p className="text-sm text-fg-muted">No golden values.</p>
                ) : (
                  <dl className="divide-y divide-border text-sm">
                    {golden.map(([k, v]) => {
                      const trail = p.survivorship?.[k] as { rule?: string; source_system?: string; source?: string } | string | undefined;
                      const rule = typeof trail === "string" ? trail : trail?.rule;
                      const from = typeof trail === "object" ? (trail.source_system ?? trail.source) : undefined;
                      return (
                        <div key={k} className="flex flex-wrap items-baseline justify-between gap-2 py-2">
                          <dt className="text-fg-muted">{titleCase(k.replace(/_/g, " "))}</dt>
                          <dd className="text-right">
                            <span className="font-medium">{displayValue(v)}</span>
                            {(rule || from) && (
                              <span className="ml-2 text-[11px] text-fg-subtle">
                                {rule ? titleCase(rule.replace(/_/g, " ")) : ""}
                                {from ? ` · ${from}` : ""}
                              </span>
                            )}
                          </dd>
                        </div>
                      );
                    })}
                  </dl>
                )}
              </CardContent>
            </Card>
            <Card>
              <CardHeader title="Segments and matches" />
              <CardContent className="space-y-4">
                <div className="flex flex-wrap gap-1.5">
                  {p.segments.length === 0 ? (
                    <span className="text-sm text-fg-muted">In no segment.</span>
                  ) : (
                    p.segments.map((s) => (
                      <Badge key={s.id} tone={s.method === "rfm" ? "teal" : "accent"} size="md">
                        <Layers /> {s.name}
                      </Badge>
                    ))
                  )}
                </div>
                {p.matches.length > 0 && (
                  <ul className="space-y-1 text-sm">
                    {p.matches.slice(0, 10).map((m) => (
                      <li key={m.id} className="flex items-center justify-between gap-2">
                        <ScoreBar value={m.score} />
                        <StatusBadge status={m.status} />
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
            <Card className="lg:col-span-2">
              <CardHeader title="Source records" description="The records identity resolution linked to this customer." />
              <CardContent className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-xs text-fg-muted">
                    <tr>
                      <th className="py-2 pr-3 text-left font-medium">Source</th>
                      <th className="py-2 pr-3 text-left font-medium">Key</th>
                      <th className="py-2 pr-3 text-left font-medium">Attributes</th>
                      <th className="py-2 text-left font-medium">Recorded</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.records.map((r) => (
                      <tr key={r.id} className="border-t border-border align-top">
                        <td className="py-2 pr-3">
                          <Badge tone="outline">{r.source_system}</Badge>
                        </td>
                        <td className="py-2 pr-3 font-mono text-xs">{r.source_key}</td>
                        <td className="py-2 pr-3 text-xs text-fg-muted">
                          {Object.entries(r.attributes ?? {})
                            .filter(([, v]) => v !== null && v !== "")
                            .map(([k, v]) => `${k}: ${displayValue(v)}`)
                            .join(" · ")}
                        </td>
                        <td className="py-2 text-xs text-fg-muted">{formatRelative(r.recorded_at ?? null)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          </div>
        </TabsContent>
        <TabsContent value="timeline">
          <Timeline id={p.id} />
        </TabsContent>
        <TabsContent value="scores">
          <Scores id={p.id} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Timeline({ id }: { id: string }) {
  const { slug } = useOrgContext();
  const [type, setType] = React.useState("");
  const t = useCustomerTimeline(slug, id, type);
  const types = React.useMemo(() => Array.from(new Set((t.items ?? []).map((e) => e.event_type))).sort(), [t.items]);
  if (t.isError) return <Card><ModuleError error={t.error} what="The timeline" onRetry={() => void t.refetch()} /></Card>;
  return (
    <Card>
      <CardHeader
        title="Timeline"
        description="Events from the mapped event sources, newest first."
        actions={
          <NativeSelect value={type} onChange={(e) => setType(e.target.value)} aria-label="Event type" className="w-auto">
            <option value="">All events</option>
            {Array.from(new Set([...types, ...(type ? [type] : [])])).map((x) => (
              <option key={x} value={x}>
                {titleCase(x.replace(/_/g, " "))}
              </option>
            ))}
          </NativeSelect>
        }
      />
      <CardContent>
        {t.isLoading ? (
          <Skeleton className="h-48" />
        ) : !t.items?.length ? (
          <EmptyState compact icon={<CalendarClock />} title="No events" description="Map an event source (orders, tickets) linked to this customer's profile source." />
        ) : (
          <ol className="relative space-y-3 border-l border-border pl-5">
            {t.items.map((e) => (
              <li key={e.id} className="relative">
                <span className="absolute top-1.5 -left-[25px] size-2.5 rounded-full border-2 border-surface bg-accent" aria-hidden />
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{titleCase(e.event_type.replace(/_/g, " "))}</span>
                  {e.amount !== null && <span className="tabular text-fg">{formatNumber(e.amount)}</span>}
                  {e.channel && <Badge tone="outline">{e.channel}</Badge>}
                  {e.source_system && <Badge tone="neutral">{e.source_system}</Badge>}
                  <time className="text-xs text-fg-subtle" dateTime={e.occurred_at}>
                    {formatDateTime(e.occurred_at)}
                  </time>
                </div>
                {Object.keys(e.attributes ?? {}).length > 0 && (
                  <p className="mt-0.5 text-xs text-fg-muted">
                    {Object.entries(e.attributes)
                      .map(([k, v]) => `${k}: ${displayValue(v)}`)
                      .join(" · ")}
                  </p>
                )}
              </li>
            ))}
          </ol>
        )}
        <Pager page={t.page} hasPrev={t.hasPrev} hasNext={t.hasNext} onPrev={t.goPrev} onNext={t.goNext} loading={t.isFetching} />
      </CardContent>
    </Card>
  );
}

function Scores({ id }: { id: string }) {
  const { slug } = useOrgContext();
  const s = useCustomerScores(slug, id);
  if (s.isError) return <Card><ModuleError error={s.error} what="Scores" onRetry={() => void s.refetch()} /></Card>;
  if (!s.data) return <Skeleton className="h-48 rounded-card" />;
  if (!s.data.latest.length) {
    return (
      <Card>
        <EmptyState icon={<Gauge />} title="Not scored yet" description="Run churn or CLV scoring from Customer 360 → Runs & scoring once a model is promoted in the ML workspace." />
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {s.data.latest.map((score) => (
          <ScoreCard key={score.id} score={score} />
        ))}
      </div>
      {s.data.history.length > s.data.latest.length && (
        <Card>
          <CardHeader title="History" />
          <CardContent>
            <ul className="divide-y divide-border text-sm">
              {s.data.history.map((h) => (
                <li key={h.id} className="flex items-center justify-between py-1.5">
                  <span>{SCORE_LABELS[h.score_type] ?? titleCase(h.score_type)}</span>
                  <span className="tabular font-medium">{scoreDisplay(h)}</span>
                  <span className="text-xs text-fg-subtle">{formatDateTime(h.scored_at)}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function ScoreCard({ score }: { score: CustomerScore }) {
  const { slug } = useOrgContext();
  const ex = explanationOf(score.explanation);
  const max = Math.max(...ex.contributions.map((c) => Math.abs(c.contribution)), 1e-9);
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-xs text-fg-muted">{SCORE_LABELS[score.score_type] ?? titleCase(score.score_type)}</p>
          <p className="tabular text-2xl font-semibold">{scoreDisplay(score)}</p>
        </div>
        <div className="text-right text-[11px] text-fg-subtle">
          {score.confidence !== null && <p>confidence {formatPercent(score.confidence, 0)}</p>}
          <p>{formatRelative(score.scored_at)}</p>
          {score.model_id && (
            <Link href={`/o/${slug}/ml/models/${score.model_id}`} className="text-accent hover:underline">
              model
            </Link>
          )}
        </div>
      </div>
      {ex.text && <p className="mt-2 text-sm text-fg-muted">{ex.text}</p>}
      {ex.reasons.length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-4 text-sm text-fg-muted">
          {ex.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      {ex.contributions.length > 0 && (
        <div className="mt-3 space-y-1">
          <p className="text-[11px] font-medium text-fg-muted">Why: top contributions</p>
          {ex.contributions.slice(0, 6).map((c) => (
            <div key={c.feature} className="grid grid-cols-[8rem_1fr_3.5rem] items-center gap-2 text-xs">
              <span className="truncate" title={c.feature}>
                {c.feature}
                {c.value !== undefined ? ` = ${c.value}` : ""}
              </span>
              <span className="h-1.5 rounded-full bg-surface-2">
                <span className={cn("block h-full rounded-full", c.contribution >= 0 ? "bg-danger/70" : "bg-success/70")} style={{ width: `${(Math.abs(c.contribution) / max) * 100}%` }} />
              </span>
              <span className="tabular text-right">{c.contribution >= 0 ? "+" : "−"}{Math.abs(c.contribution).toFixed(3)}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
