"use client";

import * as React from "react";
import { Building2, Pause, Play, Search } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { errorMessage } from "@/lib/errors";
import { orgStatusTone } from "@/lib/platform";
import { useOrgStatusMutation, usePlatformOrg, usePlatformOrgs, type PlatformOrg } from "@/lib/queries/platform";
import { formatBytes, formatDate, formatDateTime, formatNumber, formatRelative, titleCase } from "@/lib/utils";
import { useHiddenGuard } from "./platform-console";

export function OrgsPanel({ onHidden }: { onHidden: () => void }) {
  const [status, setStatus] = React.useState("all");
  const [searchText, setSearchText] = React.useState("");
  const [search, setSearch] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setSearch(searchText.trim()), 300);
    return () => clearTimeout(t);
  }, [searchText]);
  const q = usePlatformOrgs({ status: status === "all" ? "" : status, search });
  useHiddenGuard(q.error, onHidden);
  const rows = (q.data?.pages ?? []).flatMap((p) => p.items);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [acting, setActing] = React.useState<{ org: PlatformOrg; action: "suspend" | "reactivate" } | null>(null);
  const [reason, setReason] = React.useState("");
  const mut = useOrgStatusMutation();

  const confirm = async () => {
    if (!acting) return;
    try {
      await mut.mutateAsync({ id: acting.org.id, action: acting.action, reason: reason.trim() });
      toast.success(acting.action === "suspend" ? `${acting.org.name} suspended` : `${acting.org.name} reactivated`);
      setActing(null);
      setReason("");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
      <Card>
        <CardHeader icon={<Building2 />} title="Organizations" description="Members, datasets, storage and AI requests per organization." />
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-fg-subtle" aria-hidden />
              <Input className="w-64 pl-8" placeholder="Search name or slug" aria-label="Search organizations" value={searchText} onChange={(e) => setSearchText(e.target.value)} maxLength={100} />
            </div>
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger className="w-40" aria-label="Organization status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All statuses</SelectItem>
                <SelectItem value="active">Active</SelectItem>
                <SelectItem value="suspended">Suspended</SelectItem>
                <SelectItem value="deleted">Deleted</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {q.isError ? (
            <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
          ) : q.isLoading ? (
            <Skeleton className="h-40" />
          ) : rows.length === 0 ? (
            <EmptyState compact icon={<Building2 />} title="No organizations match" />
          ) : (
            <div className="scrollbar-thin overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <caption className="sr-only">Organizations</caption>
                <thead className="text-xs text-fg-muted">
                  <tr className="border-b border-border">
                    <th className="py-2 pr-3 text-left font-medium">Organization</th>
                    <th className="py-2 pr-3 text-left font-medium">Status</th>
                    <th className="py-2 pr-3 text-right font-medium">Members</th>
                    <th className="py-2 pr-3 text-right font-medium">Datasets</th>
                    <th className="py-2 pr-3 text-right font-medium">Storage</th>
                    <th className="py-2 pr-3 text-right font-medium">AI requests</th>
                    <th className="py-2 text-right font-medium">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((o) => (
                    <tr key={o.id} className={`border-b border-border/60 last:border-0 ${selected === o.id ? "bg-surface-2" : ""}`}>
                      <td className="py-2 pr-3">
                        <button type="button" className="text-left focus-ring rounded-sm" onClick={() => setSelected(o.id)} aria-label={`Show details of ${o.name}`}>
                          <div className="font-medium text-fg hover:underline">{o.name}</div>
                          <div className="font-mono text-xs text-fg-subtle">
                            {o.slug} · {o.plan} · {o.region}
                          </div>
                        </button>
                      </td>
                      <td className="py-2 pr-3">
                        <Badge tone={orgStatusTone(o.status)}>{o.status}</Badge>
                      </td>
                      <td className="tabular py-2 pr-3 text-right">{formatNumber(o.members_count)}</td>
                      <td className="tabular py-2 pr-3 text-right">{formatNumber(o.datasets_count)}</td>
                      <td className="tabular py-2 pr-3 text-right">{formatBytes(o.storage_bytes)}</td>
                      <td className="tabular py-2 pr-3 text-right" title={`Last ${o.ai_usage.window_days} days · ${formatNumber(o.ai_usage.tokens)} tokens`}>
                        {formatNumber(o.ai_usage.requests)}
                      </td>
                      <td className="py-2 text-right">
                        {o.status === "active" ? (
                          <Button size="xs" variant="danger-ghost" onClick={() => setActing({ org: o, action: "suspend" })}>
                            <Pause /> Suspend
                          </Button>
                        ) : o.status === "suspended" ? (
                          <Button size="xs" variant="secondary" onClick={() => setActing({ org: o, action: "reactivate" })}>
                            <Play /> Reactivate
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {q.hasNextPage && (
            <Button variant="secondary" size="sm" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>
              Load more
            </Button>
          )}
        </CardContent>
      </Card>
      <OrgDetail id={selected} onHidden={onHidden} />
      <ConfirmDialog
        open={!!acting}
        onOpenChange={(o) => {
          if (!o) {
            setActing(null);
            setReason("");
          }
        }}
        title={acting?.action === "suspend" ? `Suspend ${acting.org.name}?` : `Reactivate ${acting?.org.name ?? ""}?`}
        description={
          acting?.action === "suspend"
            ? "Members get 403 org_suspended on every request and the organization's API keys stop working until it is reactivated. Data is kept."
            : "Members regain access and API keys work again."
        }
        tone={acting?.action === "suspend" ? "danger" : "default"}
        confirmLabel={acting?.action === "suspend" ? "Suspend organization" : "Reactivate"}
        confirmText={acting?.action === "suspend" ? acting.org.slug : undefined}
        loading={mut.isPending}
        onConfirm={() => {
          if (acting?.action === "suspend" && !reason.trim()) {
            toast.error("Give a reason for the suspension.");
            return;
          }
          return confirm();
        }}
      >
        <Field id="pf-reason" label={acting?.action === "suspend" ? "Reason (required, audited)" : "Reason (optional, audited)"}>
          <Textarea value={reason} maxLength={500} rows={2} onChange={(e) => setReason(e.target.value)} />
        </Field>
      </ConfirmDialog>
    </div>
  );
}

function OrgDetail({ id, onHidden }: { id: string | null; onHidden: () => void }) {
  const q = usePlatformOrg(id);
  useHiddenGuard(q.error, onHidden);
  if (!id) {
    return (
      <Card className="self-start">
        <EmptyState compact icon={<Building2 />} title="Select an organization" description="Its breakdowns and platform actions appear here." />
      </Card>
    );
  }
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  if (q.isLoading || !q.data) return <Skeleton className="h-80" />;
  const d = q.data;
  const groups: Array<[string, Record<string, number>]> = [
    ["Members by role", d.members_by_role],
    ["Datasets", d.datasets_by_status],
    ["Documents", d.documents_by_status],
    ["Pipelines", d.pipelines_by_status],
    ["Jobs", d.jobs_by_status],
  ];
  const aiTotal = d.ai_daily.reduce((s, x) => s + x.requests, 0);
  return (
    <Card className="self-start">
      <CardHeader title={d.name} description={`${d.slug} · created ${formatDate(d.created_at)} · last activity ${d.last_activity_at ? formatRelative(d.last_activity_at) : "never"}`} actions={<Badge tone={orgStatusTone(d.status)}>{d.status}</Badge>} />
      <CardContent className="space-y-4 text-sm">
        {d.suspended_reason && <p className="rounded-control bg-warning-soft px-3 py-2 text-xs text-warning">Suspended: {d.suspended_reason}</p>}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
          {groups.map(([label, map]) => (
            <div key={label}>
              <dt className="text-xs text-fg-subtle">{label}</dt>
              <dd className="mt-0.5 flex flex-wrap gap-1">
                {Object.keys(map).length === 0 ? (
                  <span className="text-xs text-fg-muted">none</span>
                ) : (
                  Object.entries(map).map(([k, v]) => (
                    <Badge key={k} tone="outline">
                      {titleCase(k.replace(/_/g, " "))} · {formatNumber(v)}
                    </Badge>
                  ))
                )}
              </dd>
            </div>
          ))}
          <div>
            <dt className="text-xs text-fg-subtle">AI (last {d.ai_usage.window_days} days)</dt>
            <dd className="tabular mt-0.5 text-fg">
              {formatNumber(aiTotal)} requests · {formatNumber(d.ai_usage.tokens, { compact: true })} tokens
            </dd>
          </div>
        </dl>
        <div>
          <div className="mb-1.5 text-xs font-medium text-fg-muted">Platform actions</div>
          {d.platform_actions.length === 0 ? (
            <p className="text-xs text-fg-muted">None.</p>
          ) : (
            <ul className="space-y-1.5">
              {d.platform_actions.map((a, i) => (
                <li key={i} className="text-xs">
                  <span className="font-medium text-fg">{a.action}</span> <span className="text-fg-muted">by {a.actor_email ?? "system"}</span>{" "}
                  <span className="text-fg-subtle" title={formatDateTime(a.occurred_at)}>
                    {formatRelative(a.occurred_at)}
                  </span>
                  {a.reason && <div className="text-fg-muted">“{a.reason}”</div>}
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
