"use client";

import * as React from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Bot, Gauge, Pencil } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { CHART_COLORS, ChartCard, ChartTooltip, chartTheme } from "@/components/ui/chart-card";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { errorMessage } from "@/lib/errors";
import { parseQuota, usageByDayAndModel, type QuotaDraft } from "@/lib/platform";
import { usePlatformAiUsage, useSetModelQuota, type ModelQuota } from "@/lib/queries/platform";
import { formatDate, formatNumber, formatRelative } from "@/lib/utils";
import { Stat } from "./health-jobs";
import { useHiddenGuard } from "./platform-console";

const shortDay = (d: string) => formatDate(d).replace(/,\s*\d{4}$/, "");

export function AiPanel({ onHidden }: { onHidden: () => void }) {
  const [days, setDays] = React.useState(7);
  const q = usePlatformAiUsage(days);
  useHiddenGuard(q.error, onHidden);
  const [editing, setEditing] = React.useState<ModelQuota | null>(null);
  const d = q.data;
  const chart = React.useMemo(() => usageByDayAndModel(d?.daily ?? []), [d]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={String(days)} onValueChange={(v) => setDays(Number(v))}>
          <SelectTrigger className="w-40" aria-label="AI usage range">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {[1, 7, 30, 90].map((n) => (
              <SelectItem key={n} value={String(n)}>
                Last {n === 1 ? "24 hours" : `${n} days`}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {d && (
          <Badge tone={d.configured ? "success" : "warning"}>
            {d.configured ? `Gemini configured · ${d.tier} tier` : "Gemini key not configured"}
          </Badge>
        )}
      </div>
      {q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : q.isLoading || !d ? (
        <Skeleton className="h-72" />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <Stat label="Requests" value={formatNumber(d.totals.requests)} />
            <Stat label="Input tokens" value={formatNumber(d.totals.input_tokens, { compact: true })} />
            <Stat label="Output tokens" value={formatNumber(d.totals.output_tokens, { compact: true })} />
            <Stat label="Blocked" value={formatNumber(d.totals.blocked)} />
            <Stat label="Errors" value={formatNumber(d.totals.errors)} tone={d.totals.errors ? "danger" : undefined} />
          </div>
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
            <ChartCard
              title="Requests per day by model"
              loading={false}
              height={260}
              empty={chart.rows.length === 0 ? <EmptyState compact icon={<Bot />} title="No AI requests in this range" /> : undefined}
              table={{ columns: [{ key: "day", label: "Day" }, ...chart.models.map((m) => ({ key: m, label: m, align: "right" as const, format: (v: unknown) => formatNumber(Number(v)) }))], rows: chart.rows }}
            >
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chart.rows} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                  <CartesianGrid {...chartTheme.grid} />
                  <XAxis dataKey="day" {...chartTheme.axis} tickFormatter={shortDay} minTickGap={24} />
                  <YAxis {...chartTheme.axis} allowDecimals={false} tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
                  <Tooltip cursor={chartTheme.tooltipCursor} content={<ChartTooltip formatter={(v) => formatNumber(Number(v))} />} labelFormatter={(l) => formatDate(String(l))} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  {chart.models.map((m, i) => (
                    <Bar key={m} dataKey={m} name={m} stackId="m" fill={CHART_COLORS[i % CHART_COLORS.length]} animationDuration={500} />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>
            <Card>
              <CardHeader title="Top organizations" description={`By requests, last ${d.range.days} days.`} />
              <CardContent>
                {d.top_orgs.length === 0 ? (
                  <p className="text-sm text-fg-muted">No requests.</p>
                ) : (
                  <ol className="space-y-1.5 text-sm">
                    {d.top_orgs.map((o) => (
                      <li key={o.org_id} className="flex justify-between gap-3">
                        <span className="font-mono text-xs">{o.org_slug ?? o.org_id.slice(0, 8)}</span>
                        <span className="tabular">{formatNumber(o.requests)}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </CardContent>
            </Card>
          </div>
          <Card>
            <CardHeader icon={<Gauge />} title="Model quotas" description="Gemini rate limits per model. The effective limits are what the gateway enforces." />
            <CardContent>
              {d.models.length === 0 ? (
                <EmptyState compact icon={<Gauge />} title="No models configured" />
              ) : (
                <div className="scrollbar-thin overflow-x-auto">
                  <table className="w-full min-w-[720px] text-sm">
                    <caption className="sr-only">Model quotas</caption>
                    <thead className="text-xs text-fg-muted">
                      <tr className="border-b border-border">
                        <th className="py-2 pr-3 text-left font-medium">Model</th>
                        <th className="py-2 pr-3 text-right font-medium">RPM</th>
                        <th className="py-2 pr-3 text-right font-medium">TPM</th>
                        <th className="py-2 pr-3 text-right font-medium">RPD</th>
                        <th className="py-2 pr-3 text-left font-medium">Verified</th>
                        <th className="py-2 text-right font-medium">
                          <span className="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.models.map((m) => (
                        <tr key={m.model} className="border-b border-border/60 last:border-0">
                          <td className="py-2 pr-3">
                            <div className="font-mono text-xs text-fg">{m.model}</div>
                            {m.notes && <div className="text-xs text-fg-subtle">{m.notes}</div>}
                          </td>
                          {(["rpm", "tpm", "rpd"] as const).map((k) => (
                            <td key={k} className="tabular py-2 pr-3 text-right" title={m[k] === null ? "Default limit" : "Configured"}>
                              {formatNumber(m.effective?.[k] ?? m[k])}
                              {m[k] === null && <span className="ml-1 text-xs text-fg-subtle">default</span>}
                            </td>
                          ))}
                          <td className="py-2 pr-3 text-xs text-fg-muted">{m.verified_at ? formatRelative(m.verified_at) : <Badge tone="warning">unverified</Badge>}</td>
                          <td className="py-2 text-right">
                            <Button size="xs" variant="secondary" onClick={() => setEditing(m)} aria-label={`Edit quota of ${m.model}`}>
                              <Pencil /> Edit
                            </Button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
      <QuotaDialog quota={editing} onClose={() => setEditing(null)} />
    </div>
  );
}

function QuotaDialog({ quota, onClose }: { quota: ModelQuota | null; onClose: () => void }) {
  const [draft, setDraft] = React.useState<QuotaDraft>({ rpm: "", tpm: "", rpd: "", notes: "" });
  const [touched, setTouched] = React.useState(false);
  const mut = useSetModelQuota();
  React.useEffect(() => {
    if (!quota) return;
    const v = (k: "rpm" | "tpm" | "rpd") => String(quota[k] ?? quota.effective?.[k] ?? "");
    setDraft({ rpm: v("rpm"), tpm: v("tpm"), rpd: v("rpd"), notes: quota.notes ?? "" });
    setTouched(false);
  }, [quota]);
  const { errors, body } = parseQuota(draft);
  const set = (k: keyof QuotaDraft) => (e: React.ChangeEvent<HTMLInputElement>) => setDraft((d) => ({ ...d, [k]: e.target.value }));

  const save = async () => {
    setTouched(true);
    if (!quota || !body) return;
    try {
      await mut.mutateAsync({ model: quota.model, body });
      toast.success(`Quota for ${quota.model} saved`);
      onClose();
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <Dialog open={!!quota} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader icon={<Gauge />} title={`Model quota · ${quota?.model ?? ""}`} description="Set the limits from your Gemini project's quota page. Saving marks them as verified now; the change is audited." />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <DialogBody className="grid gap-3 sm:grid-cols-3">
          <Field id="q-rpm" label="Requests / minute" error={touched ? errors.rpm : undefined}>
            <Input inputMode="numeric" value={draft.rpm} onChange={set("rpm")} />
          </Field>
          <Field id="q-tpm" label="Tokens / minute" error={touched ? errors.tpm : undefined}>
            <Input inputMode="numeric" value={draft.tpm} onChange={set("tpm")} />
          </Field>
          <Field id="q-rpd" label="Requests / day" error={touched ? errors.rpd : undefined}>
            <Input inputMode="numeric" value={draft.rpd} onChange={set("rpd")} />
          </Field>
          <Field id="q-notes" label="Notes" className="sm:col-span-3" error={touched ? errors.notes : undefined}>
            <Input value={draft.notes} maxLength={200} onChange={set("notes")} />
          </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={mut.isPending}>
              Save quota
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
