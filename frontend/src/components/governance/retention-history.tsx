"use client";

import * as React from "react";
import Link from "next/link";
import { FlaskConical, History, Play, Plus, ShieldAlert, Timer, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Switch } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { isApiError } from "@/lib/api";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import {
  useAccessHistory,
  useRetentionMutations,
  useRetentionPolicies,
  useRetentionTargets,
  type RetentionAction,
  type RetentionPolicy,
  type RetentionTarget,
} from "@/lib/queries/governance";
import type { Dataset, Member } from "@/lib/types";
import { formatDateTime, formatNumber, formatRelative, titleCase } from "@/lib/utils";

/** Mirrors `apps/governance/retention.MAX_RETAIN_DAYS` (server validates too). */
const MAX_RETAIN_DAYS = 36500;

/* ---------------- retention ---------------- */

export function RetentionPanel() {
  const { slug } = useOrgContext();
  const q = useRetentionPolicies(slug);
  const targets = useRetentionTargets(slug);
  const m = useRetentionMutations(slug);
  const items = React.useMemo(() => targets.data?.items ?? [], [targets.data]);
  const used = new Set((q.data ?? []).map((p) => p.target));
  const free = items.filter((t) => !used.has(t.key));
  const [target, setTarget] = React.useState<string>("");
  const [name, setName] = React.useState("");
  const [days, setDays] = React.useState("365");
  const [action, setAction] = React.useState<RetentionAction>("delete");
  const [deleting, setDeleting] = React.useState<RetentionPolicy | null>(null);
  const [applying, setApplying] = React.useState<RetentionPolicy | null>(null);
  const chosen = items.find((t) => t.key === target);
  const daysN = Number(days);
  const daysError = !Number.isInteger(daysN) || daysN < 1 || daysN > MAX_RETAIN_DAYS ? `1 to ${formatNumber(MAX_RETAIN_DAYS)} days.` : undefined;

  React.useEffect(() => {
    if (chosen && !chosen.actions.includes(action)) setAction(chosen.actions[0]);
  }, [chosen, action]);

  if ((q.isError && isUnavailableEndpoint(q.error)) || (targets.isError && isUnavailableEndpoint(targets.error))) {
    return (
      <Card>
        <EmptyState icon={<Timer />} title="Retention isn't available yet" description="This server doesn't provide retention policies yet. Nothing is simulated." />
      </Card>
    );
  }

  const label = (key: string) => items.find((t) => t.key === key)?.label ?? titleCase(key.replace(/[._]/g, " "));

  const add = async () => {
    if (!chosen) return;
    try {
      await m.create.mutateAsync({ name: name.trim() || `${chosen.label} retention`, target: chosen.key, retain_days: daysN, action });
      toast.success("Retention policy added in dry-run mode");
      setTarget("");
      setName("");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const runDry = async (p: RetentionPolicy) => {
    try {
      const r = await m.dryRun.mutateAsync(p.id);
      toast.success(`Dry run: ${formatNumber(r.dry_run.matched)} item(s) would be ${r.dry_run.action === "archive" ? "archived" : "deleted"}`);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const setMode = async (p: RetentionPolicy, enforce: boolean) => {
    try {
      await m.update.mutateAsync({ id: p.id, body: { mode: enforce ? "enforce" : "dry_run" } });
      toast.success(enforce ? "The hourly job now enforces this policy" : "Back to dry-run mode");
    } catch (e) {
      toast.error(isApiError(e) && e.code === "retention_dry_run_required" ? "Run a dry run with the current settings before enforcing." : errorMessage(e));
    }
  };

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader
          icon={<Timer />}
          title="Retention policies"
          description="Data older than the window is deleted or archived by the hourly governance job. New policies start in dry-run mode: the job only counts what it would touch until you enforce it."
        />
        <CardContent className="space-y-4">
          {q.isError ? (
            <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
          ) : q.isLoading ? (
            <Skeleton className="h-16" />
          ) : (q.data ?? []).length === 0 ? (
            <EmptyState compact icon={<Timer />} title="No retention policies" description="Data is kept until it is deleted manually." />
          ) : (
            <ul className="divide-y divide-border rounded-control border border-border" aria-label="Retention policies">
              {(q.data ?? []).map((p) => (
                <RetentionRow
                  key={p.id}
                  policy={p}
                  targetLabel={label(p.target)}
                  dryRunning={m.dryRun.isPending && m.dryRun.variables === p.id}
                  modeSaving={m.update.isPending && m.update.variables?.id === p.id}
                  onDryRun={() => void runDry(p)}
                  onMode={(enforce) => void setMode(p, enforce)}
                  onApply={() => setApplying(p)}
                  onDelete={() => setDeleting(p)}
                />
              ))}
            </ul>
          )}
          <NewRetentionForm
            free={free}
            loadingTargets={targets.isLoading}
            target={target}
            setTarget={setTarget}
            name={name}
            setName={setName}
            days={days}
            setDays={setDays}
            daysError={daysError}
            action={action}
            setAction={setAction}
            chosen={chosen}
            saving={m.create.isPending}
            onAdd={() => void add()}
          />
          {(targets.data?.never ?? []).length > 0 && (
            <p className="text-xs text-fg-muted">
              Never deleted by retention (append-only evidence): {(targets.data?.never ?? []).map((n) => titleCase(n.replace(/[._]/g, " "))).join(", ")}.
            </p>
          )}
        </CardContent>
      </Card>
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete this retention policy?"
        description="Data covered by it will be kept until deleted manually."
        confirmLabel="Delete policy"
        loading={m.remove.isPending}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await m.remove.mutateAsync(deleting.id);
            setDeleting(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
      <ConfirmDialog
        open={!!applying}
        onOpenChange={(o) => !o && setApplying(null)}
        title={`${applying?.action === "archive" ? "Archive" : "Delete"} matching data now?`}
        description={
          applying?.last_dry_run
            ? `The last dry run matched ${formatNumber(applying.last_dry_run.matched)} item(s) older than ${formatDateTime(applying.last_dry_run.cutoff)}. This cannot be undone.`
            : "This cannot be undone."
        }
        confirmLabel={applying?.action === "archive" ? "Archive now" : "Delete now"}
        confirmText={applying?.action === "delete" ? "delete" : undefined}
        loading={m.apply.isPending}
        onConfirm={async () => {
          if (!applying) return;
          try {
            const r = await m.apply.mutateAsync(applying.id);
            if (r.result.skipped === "legal_hold") toast.warning("Legal hold is active: nothing was deleted or archived.");
            else toast.success(`${formatNumber(r.result.affected)} item(s) ${r.result.action === "archive" ? "archived" : "deleted"}`);
            setApplying(null);
          } catch (e) {
            toast.error(isApiError(e) && e.code === "retention_dry_run_required" ? "Run a dry run first." : errorMessage(e));
          }
        }}
      />
    </div>
  );
}

function RetentionRow({
  policy: p,
  targetLabel,
  dryRunning,
  modeSaving,
  onDryRun,
  onMode,
  onApply,
  onDelete,
}: {
  policy: RetentionPolicy;
  targetLabel: string;
  dryRunning: boolean;
  modeSaving: boolean;
  onDryRun: () => void;
  onMode: (enforce: boolean) => void;
  onApply: () => void;
  onDelete: () => void;
}) {
  const dry = p.last_dry_run;
  const last = p.last_result;
  return (
    <li className="space-y-2 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium text-fg">{p.name}</span>
        <Badge tone="outline">{targetLabel}</Badge>
        <span className="text-fg-muted">
          {titleCase(p.action)} after {formatNumber(p.retain_days)} days
        </span>
        {!p.is_active && <Badge tone="neutral">Paused</Badge>}
        <Badge tone={p.mode === "enforce" ? "warning" : "info"}>{p.mode === "enforce" ? "Enforced" : "Dry run only"}</Badge>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-fg-muted">
            <Switch checked={p.mode === "enforce"} disabled={modeSaving} onCheckedChange={onMode} aria-label={`Enforce ${p.name}`} />
            Enforce
          </label>
          <Button size="xs" variant="secondary" onClick={onDryRun} loading={dryRunning}>
            <FlaskConical /> Dry run
          </Button>
          <Button size="xs" variant="secondary" onClick={onApply} disabled={!p.last_dry_run_at}>
            <Play /> Apply now
          </Button>
          <Button size="icon-xs" variant="ghost" aria-label={`Delete retention policy ${p.name}`} onClick={onDelete}>
            <Trash2 />
          </Button>
        </div>
      </div>
      {dry ? (
        <div className="rounded-control bg-surface-2 px-3 py-2 text-xs" aria-label="Last dry run">
          <div className="mb-1 flex flex-wrap items-center gap-2 font-medium text-fg">
            Last dry run {formatRelative(dry.at)}, nothing was changed
            {dry.legal_hold && (
              <Badge tone="warning">
                <ShieldAlert className="size-3" /> Legal hold
              </Badge>
            )}
          </div>
          <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-3">
            <div>
              <dt className="text-fg-subtle">Would {dry.action}</dt>
              <dd className="tabular text-fg">{formatNumber(dry.matched)} item(s)</dd>
            </div>
            <div>
              <dt className="text-fg-subtle">Older than</dt>
              <dd className="tabular text-fg">{formatDateTime(dry.cutoff)}</dd>
            </div>
            {last && (
              <div>
                <dt className="text-fg-subtle">Last applied ({last.trigger})</dt>
                <dd className="tabular text-fg">
                  {last.skipped === "legal_hold" ? "skipped: legal hold" : `${formatNumber(last.affected)} ${last.action === "archive" ? "archived" : "deleted"}`} · {formatRelative(last.at)}
                </dd>
              </div>
            )}
          </dl>
        </div>
      ) : (
        <p className="text-xs text-fg-muted">No dry run with the current settings yet. Run one before enforcing or applying.</p>
      )}
    </li>
  );
}

function NewRetentionForm(props: {
  free: RetentionTarget[];
  loadingTargets: boolean;
  target: string;
  setTarget: (v: string) => void;
  name: string;
  setName: (v: string) => void;
  days: string;
  setDays: (v: string) => void;
  daysError: string | undefined;
  action: RetentionAction;
  setAction: (v: RetentionAction) => void;
  chosen: RetentionTarget | undefined;
  saving: boolean;
  onAdd: () => void;
}) {
  const { free, chosen } = props;
  if (!props.loadingTargets && free.length === 0) {
    return <p className="text-xs text-fg-muted">Every supported target already has a policy.</p>;
  }
  return (
    <div className="space-y-2 rounded-control border border-dashed border-border p-3">
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_110px_140px_auto] md:items-end">
        <Field id="rt-target" label="Applies to">
          <Select value={props.target || undefined} onValueChange={props.setTarget}>
            <SelectTrigger aria-label="Retention target">
              <SelectValue placeholder={props.loadingTargets ? "Loading…" : "Choose what to retain"} />
            </SelectTrigger>
            <SelectContent>
              {free.map((t) => (
                <SelectItem key={t.key} value={t.key} description={t.description}>
                  {t.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field id="rt-name" label="Name">
          <Input value={props.name} maxLength={120} placeholder={chosen ? `${chosen.label} retention` : "Policy name"} onChange={(e) => props.setName(e.target.value)} />
        </Field>
        <Field id="rt-days" label="Keep (days)" error={props.daysError}>
          <Input inputMode="numeric" value={props.days} onChange={(e) => props.setDays(e.target.value)} />
        </Field>
        <Field id="rt-action" label="Then">
          <Select value={props.action} onValueChange={(v) => props.setAction(v as RetentionAction)}>
            <SelectTrigger aria-label="Retention action">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(chosen?.actions ?? (["delete", "archive"] as RetentionAction[])).map((a) => (
                <SelectItem key={a} value={a}>
                  {titleCase(a)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Button onClick={props.onAdd} disabled={!!props.daysError || !chosen} loading={props.saving}>
          <Plus /> Add
        </Button>
      </div>
      {chosen?.description && <p className="text-xs text-fg-muted">{chosen.description}</p>}
    </div>
  );
}

/* ---------------- access history ---------------- */

const ACCESS_TYPES = ["view", "query", "export", "download", "predict"];
const HISTORY_ASSETS = ["dataset", "document", "model", "report", "dashboard"];

export function AccessHistoryPanel({ datasets, members }: { datasets: Dataset[]; members: Member[] }) {
  const { slug } = useOrgContext();
  const [userId, setUserId] = React.useState("all");
  const [accessType, setAccessType] = React.useState("all");
  const [assetType, setAssetType] = React.useState("all");
  const q = useAccessHistory(slug, {
    user_id: userId === "all" ? "" : userId,
    access_type: accessType === "all" ? "" : accessType,
    asset_type: assetType === "all" ? "" : assetType,
  });
  const rows = (q.data?.pages ?? []).flatMap((p) => p.items);
  const dsName = (id: string) => datasets.find((d) => d.id === id)?.name;

  return (
    <Card>
      <CardHeader icon={<History />} title="Access history" description="An append-only record of who read, queried, exported or predicted with which asset. It never stores data values." />
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2" role="group" aria-label="Filters">
          <Select value={userId} onValueChange={setUserId}>
            <SelectTrigger className="w-56" aria-label="Filter by member">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All members</SelectItem>
              {members.map((m) => (
                <SelectItem key={m.user.id} value={m.user.id}>
                  {m.user.email}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={accessType} onValueChange={setAccessType}>
            <SelectTrigger className="w-40" aria-label="Filter by access type">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All access</SelectItem>
              {ACCESS_TYPES.map((a) => (
                <SelectItem key={a} value={a}>
                  {titleCase(a)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={assetType} onValueChange={setAssetType}>
            <SelectTrigger className="w-40" aria-label="Filter by asset type">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All assets</SelectItem>
              {HISTORY_ASSETS.map((a) => (
                <SelectItem key={a} value={a}>
                  {titleCase(a)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {q.isError ? (
          <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
        ) : q.isLoading ? (
          <Skeleton className="h-32" />
        ) : rows.length === 0 ? (
          <EmptyState compact icon={<History />} title="No access recorded" description="Queries, exports and predictions appear here as they happen." />
        ) : (
          <div className="scrollbar-thin overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <caption className="sr-only">Access history</caption>
              <thead className="text-xs text-fg-muted">
                <tr className="border-b border-border">
                  <th className="py-2 pr-3 text-left font-medium">When</th>
                  <th className="py-2 pr-3 text-left font-medium">Who</th>
                  <th className="py-2 pr-3 text-left font-medium">Access</th>
                  <th className="py-2 pr-3 text-left font-medium">Asset</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-border/60 last:border-0">
                    <td className="py-1.5 pr-3 text-xs text-fg-muted" title={formatDateTime(r.occurred_at)}>
                      {formatRelative(r.occurred_at)}
                    </td>
                    <td className="py-1.5 pr-3">{r.user_email ?? (r.api_key_id ? <span className="font-mono text-xs">API key</span> : "—")}</td>
                    <td className="py-1.5 pr-3">
                      <Badge tone={r.access_type === "export" || r.access_type === "download" ? "warning" : "neutral"}>{r.access_type}</Badge>
                    </td>
                    <td className="py-1.5 pr-3">
                      {r.asset_type === "dataset" && dsName(r.asset_id) ? (
                        <Link href={`/o/${slug}/data/datasets/${r.asset_id}`} className="hover:underline">
                          {dsName(r.asset_id)}
                        </Link>
                      ) : (
                        <span className="text-fg-muted">
                          {titleCase(r.asset_type)} <span className="font-mono text-xs">{r.asset_id.slice(0, 8)}</span>
                        </span>
                      )}
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
  );
}
