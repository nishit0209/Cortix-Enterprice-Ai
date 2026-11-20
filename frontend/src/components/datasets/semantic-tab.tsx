"use client";

import * as React from "react";
import { BookOpen, Pencil, Plus, RefreshCw, RotateCcw, Save, Sigma, Trash2, Undo2, X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Switch } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status";
import { isApiError } from "@/lib/api";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import {
  semanticDiff,
  useDeleteMetric,
  useRefreshSemanticModel,
  useSaveMetric,
  useSemanticModel,
  useUpdateSemanticModel,
} from "@/lib/queries/pipelines";
import type { Dataset, MetricDefinition, SemanticColumn, SemanticTable } from "@/lib/types";
import { cn, formatRelative } from "@/lib/utils";

/** Backend roles (`apps/semantic/generation.py`). `pii` is fixed for columns detected as personal data. */
const ROLES = ["dimension", "measure", "time", "id", "pii"];
const AGGS = ["sum", "avg", "min", "max", "count", "count_distinct"];
const NONE = "__none__";

/** Tag-style input for synonyms (Enter or comma adds; Backspace on empty removes the last). */
export function SynonymInput({ value, onChange, id, disabled }: { value: string[]; onChange: (v: string[]) => void; id: string; disabled?: boolean }) {
  const [draft, setDraft] = React.useState("");
  const add = (raw: string) => {
    const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
    if (parts.length) onChange(Array.from(new Set([...value, ...parts])));
    setDraft("");
  };
  return (
    <div className="flex min-h-8 flex-wrap items-center gap-1 rounded-control border border-border bg-surface px-1.5 py-1 focus-within:border-accent">
      {value.map((s) => (
        <span key={s} className="inline-flex items-center gap-0.5 rounded-full bg-surface-2 py-0.5 pl-2 pr-1 text-[11px] text-fg">
          {s}
          {!disabled && (
            <button type="button" onClick={() => onChange(value.filter((x) => x !== s))} aria-label={`Remove ${s}`} className="rounded-full p-0.5 hover:bg-surface-3">
              <X className="size-2.5" />
            </button>
          )}
        </span>
      ))}
      <input
        id={id}
        value={draft}
        disabled={disabled}
        onChange={(e) => (e.target.value.endsWith(",") ? add(e.target.value) : setDraft(e.target.value))}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add(draft);
          } else if (e.key === "Backspace" && !draft && value.length) onChange(value.slice(0, -1));
        }}
        onBlur={() => draft && add(draft)}
        placeholder={value.length ? "" : "revenue, sales…"}
        className="min-w-16 flex-1 bg-transparent text-xs outline-none placeholder:text-fg-subtle"
      />
    </div>
  );
}

export function SemanticTab({ org, dataset, canManage }: { org: string; dataset: Dataset; canManage: boolean }) {
  const q = useSemanticModel(org, dataset.id);
  const update = useUpdateSemanticModel(org, dataset.id);
  const refresh = useRefreshSemanticModel(org, dataset.id);
  const model = q.data ?? null;
  const [tables, setTables] = React.useState<SemanticTable[]>([]);
  const [metricEdit, setMetricEdit] = React.useState<MetricDefinition | null>(null);
  const [metricDelete, setMetricDelete] = React.useState<MetricDefinition | null>(null);
  const [resetOpen, setResetOpen] = React.useState(false);
  React.useEffect(() => {
    if (model) setTables(structuredClone(model.tables));
  }, [model]);
  const patch = React.useMemo(() => (model ? semanticDiff(model, tables) : {}), [model, tables]);
  const dirty = !!(patch.tables?.length || patch.columns?.length);

  if (q.isLoading) return <Skeleton className="h-80 rounded-card" />;
  if (q.isError || !model) {
    return (
      <Card>
        {q.isError && isApiError(q.error) && q.error.code === "dataset_not_ready" ? (
          <EmptyState icon={<BookOpen />} title="The semantic model appears when the dataset is ready" description="It is generated from the profiled columns once ingestion finishes." />
        ) : q.isError && isUnavailableEndpoint(q.error) ? (
          <EmptyState icon={<BookOpen />} title="The semantic layer isn't available" description="The semantic service did not respond. Descriptions, synonyms, roles and metrics appear here once it does." />
        ) : (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        )}
      </Card>
    );
  }

  const setColumn = (ti: number, ci: number, p: Partial<SemanticColumn>) =>
    setTables((ts) => ts.map((t, i) => (i !== ti ? t : { ...t, columns: t.columns.map((c, j) => (j !== ci ? c : { ...c, ...p })) })));
  const setTable = (ti: number, p: Partial<SemanticTable>) => setTables((ts) => ts.map((t, i) => (i !== ti ? t : { ...t, ...p })));

  const doRefresh = async (reset: boolean) => {
    try {
      await refresh.mutateAsync(reset);
      toast.success(reset ? "Semantic model regenerated; manual edits were reset." : "Semantic model refreshed from the current version. Your edits were kept.");
      setResetOpen(false);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-center gap-3 px-5 py-4">
          <BookOpen className="size-4 text-fg-subtle" aria-hidden />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-fg">{model.name}</div>
            <div className="text-xs text-fg-muted">
              {model.version !== null ? `Dataset version ${model.version}` : "No ready version"}
              {model.generated_at ? ` · generated ${formatRelative(model.generated_at)}` : ""}
            </div>
          </div>
          <StatusBadge status={model.status === "ready" ? "active" : "failed"} />
          <span className="text-xs text-fg-muted">
            {model.status === "ready" ? "Ask AI uses these names, synonyms and metrics." : (model.last_error?.message ?? "Generation failed. Refresh to retry.")}
          </span>
          {canManage && (
            <div className="ml-auto flex flex-wrap gap-2">
              {dirty && (
                <Button variant="ghost" size="sm" onClick={() => setTables(structuredClone(model.tables))}>
                  <Undo2 /> Discard
                </Button>
              )}
              <Button variant="ghost" size="sm" loading={refresh.isPending && !resetOpen} disabled={dirty} onClick={() => void doRefresh(false)}>
                <RefreshCw /> Refresh
              </Button>
              <Button variant="ghost" size="sm" disabled={dirty} onClick={() => setResetOpen(true)}>
                <RotateCcw /> Reset edits
              </Button>
              <Button
                size="sm"
                disabled={!dirty}
                loading={update.isPending}
                onClick={async () => {
                  try {
                    await update.mutateAsync(patch);
                    toast.success("Semantic model saved. Ask AI uses it from the next question.");
                  } catch (e) {
                    toast.error(errorMessage(e));
                  }
                }}
              >
                <Save /> Save
              </Button>
            </div>
          )}
        </div>
      </Card>

      {tables.map((t, ti) => (
        <Card key={t.name}>
          <CardHeader title={<span className="font-mono">{t.name}</span>} description={t.display_name !== t.name ? t.display_name : undefined} />
          <CardContent className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <Field id={`tbl-desc-${ti}`} label="Table description">
                <Textarea value={t.description} disabled={!canManage} onChange={(e) => setTable(ti, { description: e.target.value })} placeholder="What one row represents" className="min-h-12 text-xs" />
              </Field>
              <div className="space-y-1.5">
                <label htmlFor={`tbl-syn-${ti}`} className="text-sm font-medium text-fg">
                  Table synonyms
                </label>
                <SynonymInput id={`tbl-syn-${ti}`} value={t.synonyms ?? []} disabled={!canManage} onChange={(v) => setTable(ti, { synonyms: v })} />
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] text-sm">
                <thead className="text-left text-xs text-fg-muted">
                  <tr className="border-b border-border">
                    <th className="py-2 pr-3 font-medium">Column</th>
                    <th className="py-2 pr-3 font-medium">Display name &amp; description</th>
                    <th className="py-2 pr-3 font-medium">Synonyms</th>
                    <th className="py-2 pr-3 font-medium">Role</th>
                    <th className="py-2 pr-3 font-medium">Default agg</th>
                    <th className="py-2 font-medium" title="Excluded columns are never named in AI prompts">
                      AI
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {t.columns.map((c, ci) => {
                    const aggAllowed = c.role === "measure" || c.role === "id";
                    return (
                      <tr key={c.name} className="border-b border-border align-top last:border-0">
                        <td className="py-2.5 pr-3">
                          <div className="font-mono text-xs text-fg">{c.name}</div>
                          <div className="mt-0.5 text-[11px] text-fg-subtle">{c.data_type ?? c.type}</div>
                          {c.is_pii && (
                            <Badge tone="warning" className="mt-1">
                              PII
                            </Badge>
                          )}
                        </td>
                        <td className="space-y-1.5 py-2 pr-3">
                          <Input aria-label={`${c.name} display name`} value={c.display_name} disabled={!canManage} onChange={(e) => setColumn(ti, ci, { display_name: e.target.value })} className="h-8 text-xs" />
                          <Textarea
                            aria-label={`${c.name} description`}
                            value={c.description}
                            disabled={!canManage}
                            onChange={(e) => setColumn(ti, ci, { description: e.target.value })}
                            placeholder="What this column means"
                            className="min-h-12 text-xs"
                          />
                        </td>
                        <td className="w-56 py-2 pr-3">
                          <SynonymInput id={`syn-${ti}-${ci}`} value={c.synonyms ?? []} disabled={!canManage} onChange={(v) => setColumn(ti, ci, { synonyms: v })} />
                        </td>
                        <td className="w-36 py-2 pr-3">
                          <Select
                            value={c.role}
                            onValueChange={(v) => setColumn(ti, ci, { role: v, ...(v !== "measure" && v !== "id" ? { default_agg: null } : {}) })}
                            disabled={!canManage || !!c.is_pii}
                          >
                            <SelectTrigger className="h-8 text-xs" aria-label={`${c.name} role`} title={c.is_pii ? "Columns with personal data keep the pii role" : undefined}>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {ROLES.filter((r) => r !== "pii" || c.is_pii || c.role === "pii").map((r) => (
                                <SelectItem key={r} value={r}>
                                  {r}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </td>
                        <td className="w-36 py-2 pr-3">
                          <Select value={c.default_agg ?? NONE} onValueChange={(v) => setColumn(ti, ci, { default_agg: v === NONE ? null : v })} disabled={!canManage || !aggAllowed}>
                            <SelectTrigger className="h-8 text-xs" aria-label={`${c.name} default aggregation`}>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value={NONE}>—</SelectItem>
                              {AGGS.map((a) => (
                                <SelectItem key={a} value={a}>
                                  {a}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </td>
                        <td className="py-3">
                          <Switch checked={c.exposed_to_ai !== false} disabled={!canManage} onCheckedChange={(v) => setColumn(ti, ci, { exposed_to_ai: v })} aria-label={`Expose ${c.name} to AI`} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      ))}

      <Card>
        <CardHeader
          icon={<Sigma />}
          title="Metrics"
          description="Reusable business measures. Expressions are validated by the SQL guard; invalid ones are not offered to Ask AI."
          actions={
            canManage ? (
              <Button size="sm" variant="secondary" onClick={() => setMetricEdit({ key: "", name: "", expression: "", description: "", synonyms: [] })}>
                <Plus /> Add metric
              </Button>
            ) : undefined
          }
        />
        <CardContent>
          {model.metrics.length === 0 ? (
            <EmptyState compact icon={<Sigma />} title="No metrics yet" description="Define metrics such as net revenue = SUM(amount) - SUM(discount)." />
          ) : (
            <ul className="divide-y divide-border">
              {model.metrics.map((m) => (
                <li key={m.id ?? m.key} className="flex flex-wrap items-start gap-3 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-fg">{m.name}</span>
                      <Badge tone="outline" className="font-mono">
                        {m.key}
                      </Badge>
                      {m.is_default && <Badge tone="neutral">auto</Badge>}
                      {m.status === "invalid" && <Badge tone="danger">invalid</Badge>}
                      {m.is_active === false && <Badge tone="outline">inactive</Badge>}
                    </div>
                    <code className="mt-1 block font-mono text-xs text-accent">{m.expression}</code>
                    {m.error && <p className="mt-1 text-xs text-danger">{m.error}</p>}
                    {m.description && <p className="mt-1 text-xs text-fg-muted">{m.description}</p>}
                    {(m.synonyms?.length ?? 0) > 0 && <p className="mt-1 text-[11px] text-fg-subtle">Also: {m.synonyms.join(", ")}</p>}
                  </div>
                  {canManage && (
                    <div className="flex gap-1">
                      <Button variant="ghost" size="icon-xs" aria-label={`Edit ${m.name}`} onClick={() => setMetricEdit(m)}>
                        <Pencil />
                      </Button>
                      <Button variant="danger-ghost" size="icon-xs" aria-label={`Delete ${m.name}`} onClick={() => setMetricDelete(m)}>
                        <Trash2 />
                      </Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
      <MetricDialog org={org} datasetId={dataset.id} metric={metricEdit} onClose={() => setMetricEdit(null)} />
      <MetricDeleteDialog org={org} datasetId={dataset.id} metric={metricDelete} onClose={() => setMetricDelete(null)} />
      <ConfirmDialog
        open={resetOpen}
        onOpenChange={setResetOpen}
        title="Reset all manual edits?"
        description="The semantic model is regenerated from the dataset profile. Descriptions, synonyms and roles you edited are replaced by the generated ones. Metrics you defined are kept."
        confirmLabel="Reset edits"
        loading={refresh.isPending}
        onConfirm={() => void doRefresh(true)}
      />
    </div>
  );
}

function MetricDialog({ org, datasetId, metric, onClose }: { org: string; datasetId: string; metric: MetricDefinition | null; onClose: () => void }) {
  const save = useSaveMetric(org, datasetId);
  const [m, setM] = React.useState<MetricDefinition | null>(metric);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  React.useEffect(() => {
    setM(metric);
    setErrors({});
  }, [metric]);
  if (!m) return <Dialog open={false} />;
  const set = (p: Partial<MetricDefinition>) => setM((x) => (x ? { ...x, ...p } : x));
  return (
    <Dialog open={!!metric} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="md">
        <DialogHeader icon={<Sigma />} title={m.id ? "Edit metric" : "Add metric"} />
        <form
          className="contents"
          onSubmit={async (e) => {
            e.preventDefault();
            const errs: Record<string, string> = {};
            if (!m.id && m.key && !/^[a-z][a-z0-9_]{0,62}$/.test(m.key)) errs.key = "Start with a letter; use lowercase letters, digits and underscores.";
            if (!m.name.trim()) errs.name = "Name is required.";
            if (!m.expression.trim()) errs.expression = "Expression is required.";
            setErrors(errs);
            if (Object.keys(errs).length) return;
            try {
              await save.mutateAsync(m);
              toast.success("Metric saved");
              onClose();
            } catch (err) {
              if (isApiError(err)) setErrors(err.fieldErrors());
              toast.error(errorMessage(err));
            }
          }}
        >
          <DialogBody className="grid gap-4 sm:grid-cols-2">
            <Field id="m-key" label="Key" error={errors.key} hint={m.id ? undefined : "Optional; generated from the name when empty."}>
              <Input value={m.key} onChange={(e) => set({ key: e.target.value })} placeholder="net_revenue" className="font-mono" disabled={!!m.id} />
            </Field>
            <Field id="m-name" label="Name" error={errors.name}>
              <Input value={m.name} onChange={(e) => set({ name: e.target.value })} placeholder="Net revenue" />
            </Field>
            <Field id="m-expr" label="Expression" error={errors.expression} className={cn("sm:col-span-2")}>
              <Textarea value={m.expression} onChange={(e) => set({ expression: e.target.value })} placeholder="SUM(amount) - SUM(discount)" className="font-mono text-xs" spellCheck={false} />
            </Field>
            <Field id="m-desc" label="Description" className="sm:col-span-2">
              <Textarea value={m.description} onChange={(e) => set({ description: e.target.value })} />
            </Field>
            <div className="space-y-1.5 sm:col-span-2">
              <label htmlFor="m-syn" className="text-sm font-medium text-fg">
                Synonyms
              </label>
              <SynonymInput id="m-syn" value={m.synonyms ?? []} onChange={(v) => set({ synonyms: v })} />
            </div>
            {m.id && (
              <label className="flex items-center gap-2 text-sm text-fg sm:col-span-2">
                <Switch checked={m.is_active !== false} onCheckedChange={(v) => set({ is_active: v })} aria-label="Metric active" />
                Active (offered to Ask AI)
              </label>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending}>
              Save metric
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function MetricDeleteDialog({ org, datasetId, metric, onClose }: { org: string; datasetId: string; metric: MetricDefinition | null; onClose: () => void }) {
  const del = useDeleteMetric(org, datasetId);
  return (
    <ConfirmDialog
      open={!!metric}
      onOpenChange={(o) => !o && onClose()}
      title={`Delete metric “${metric?.name ?? ""}”?`}
      description="Questions and dashboards that use this metric will stop resolving it."
      confirmLabel="Delete metric"
      loading={del.isPending}
      onConfirm={async () => {
        if (!metric?.id) return onClose();
        try {
          await del.mutateAsync(metric.id);
          toast.success("Metric deleted");
          onClose();
        } catch (e) {
          toast.error(errorMessage(e));
        }
      }}
    />
  );
}
