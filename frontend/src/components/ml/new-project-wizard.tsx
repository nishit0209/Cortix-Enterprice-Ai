"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowLeft, ArrowRight, Check, Database, Info, Lock, ShieldOff, Sparkles, Unplug } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field, Label } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ClassificationBadge } from "@/components/ui/status";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { TASK_TYPES, type ColumnReason } from "@/lib/ml/types";
import {
  WIZARD_STEPS,
  blockedColumns,
  initialWizard,
  isTemporal,
  localPreview,
  stepError,
  toCreateRequest,
  wizardReducer,
  type WizardStep,
} from "@/lib/ml/wizard";
import { useDatasetColumns, useDatasets } from "@/lib/queries/datasets";
import { useCreateMlProject, useProjectPreview } from "@/lib/queries/ml";
import { cn, formatNumber } from "@/lib/utils";
import { TASK_HINTS, TASK_LABELS } from "./common";

const STEP_LABELS: Record<WizardStep, string> = { dataset: "Dataset", target: "Target", features: "Features", review: "Review" };

export function NewProjectWizard() {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const reduce = useReducedMotion();
  const [s, dispatch] = React.useReducer(wizardReducer, initialWizard);
  const datasets = useDatasets(slug);
  const ready = React.useMemo(() => (datasets.data ?? []).filter((d) => String(d.status) === "ready"), [datasets.data]);
  const columns = useDatasetColumns(slug, s.datasetId ?? "", !!s.datasetId);
  const create = useCreateMlProject(slug);

  React.useEffect(() => {
    if (columns.data && s.datasetId) {
      dispatch({
        type: "columns_loaded",
        datasetId: s.datasetId,
        columns: columns.data.map((c) => ({ name: c.name, dtype: c.data_type, distinct: c.stats?.distinct ?? null, is_pii: c.is_pii })),
      });
    }
  }, [columns.data, s.datasetId]);

  const wantPreview = s.step === "features" || s.step === "review";
  const preview = useProjectPreview(slug, { datasetId: s.datasetId, target: s.target, task: s.taskType, timeColumn: s.timeColumn, enabled: wantPreview && !s.preview });
  React.useEffect(() => {
    if (!wantPreview || s.preview || !s.columns || preview.isLoading || preview.isFetching) return;
    if (preview.data) dispatch({ type: "preview_loaded", preview: preview.data });
    else if (preview.data === null) dispatch({ type: "preview_loaded", preview: localPreview(s.columns, s.target, s.timeColumn, s.rowCount) });
  }, [wantPreview, s.preview, s.columns, s.target, s.timeColumn, s.rowCount, preview.data, preview.isLoading, preview.isFetching]);

  if (!can("ml.manage")) {
    return (
      <Card>
        <EmptyState icon={<Lock />} title="You can't create ML projects" description="Creating projects needs the ml.manage permission. Ask an owner or admin." />
      </Card>
    );
  }

  const err = stepError(s);
  const idx = WIZARD_STEPS.indexOf(s.step);

  const submit = async () => {
    const body = toCreateRequest(s);
    if (!body) return;
    try {
      const p = await create.mutateAsync(body);
      toast.success("Project created");
      router.push(`/o/${slug}/ml/projects/${p.id}`);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/ml`} className="inline-flex items-center gap-1 hover:text-fg">
            <ArrowLeft className="size-3" /> ML Workspace
          </Link>
        }
        title="New ML project"
        description="Pick a ready dataset and what to predict. Columns you may not see unmasked, and columns that would leak the answer, are excluded with a reason."
      />

      <ol className="grid grid-cols-4 gap-2" aria-label="Steps">
        {WIZARD_STEPS.map((step, i) => {
          const done = i < idx;
          const active = i === idx;
          return (
            <li key={step}>
              <button
                type="button"
                onClick={() => dispatch({ type: "goto", step })}
                aria-current={active ? "step" : undefined}
                className={cn(
                  "group flex w-full items-center gap-2 rounded-control border px-3 py-2 text-left text-sm transition-colors focus-ring",
                  active ? "border-accent bg-accent-soft text-accent-soft-fg" : done ? "border-border bg-surface text-fg" : "border-border bg-surface text-fg-subtle",
                )}
              >
                <span className={cn("flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold", done ? "bg-success text-white" : active ? "bg-accent text-accent-fg" : "bg-surface-3")}>
                  {done ? <Check className="size-3" /> : i + 1}
                </span>
                <span className="truncate">{STEP_LABELS[step]}</span>
              </button>
            </li>
          );
        })}
      </ol>

      <Card className="overflow-hidden">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={s.step}
            initial={reduce ? false : { opacity: 0, x: 12 }}
            animate={{ opacity: 1, x: 0 }}
            exit={reduce ? undefined : { opacity: 0, x: -12 }}
            transition={{ duration: 0.18 }}
            className="p-6"
          >
            {s.step === "dataset" && (
              <DatasetStep
                loading={datasets.isLoading}
                error={datasets.isError ? datasets.error : null}
                datasets={ready}
                selected={s.datasetId}
                columnsLoading={columns.isLoading && !!s.datasetId}
                columnsError={columns.isError ? columns.error : null}
                onSelect={(d) => dispatch({ type: "select_dataset", id: d.id, name: d.name, rowCount: d.row_count })}
              />
            )}
            {s.step === "target" && s.columns && (
              <div className="space-y-6">
                <div className="space-y-2">
                  <Label>Task type</Label>
                  <div className="grid gap-2 sm:grid-cols-4" role="radiogroup" aria-label="Task type">
                    {TASK_TYPES.map((t) => (
                      <button
                        key={t}
                        type="button"
                        role="radio"
                        aria-checked={s.taskType === t}
                        onClick={() => dispatch({ type: "set_task", task: t })}
                        className={cn(
                          "rounded-card border p-3 text-left transition-colors focus-ring",
                          s.taskType === t ? "border-accent bg-accent-soft/60" : "border-border hover:border-border-strong",
                        )}
                      >
                        <span className="flex items-center gap-1.5 text-sm font-medium text-fg">
                          {TASK_LABELS[t]}
                          {s.detectedTask === t && (
                            <Badge tone="accent" className="ml-auto">
                              <Sparkles /> Detected
                            </Badge>
                          )}
                        </span>
                        <span className="mt-1 block text-xs text-fg-muted">{TASK_HINTS[t]}</span>
                      </button>
                    ))}
                  </div>
                  {s.taskOverridden && s.detectedTask && (
                    <p className="text-xs text-fg-muted">
                      You overrode the detected task ({TASK_LABELS[s.detectedTask]}). The override is recorded with the project.
                    </p>
                  )}
                </div>
                {s.taskType !== "clustering" && (
                  <Field id="ml-target" label="Target column" hint="The column the model learns to predict.">
                    <Select value={s.target ?? undefined} onValueChange={(v) => dispatch({ type: "set_target", column: v })}>
                      <SelectTrigger aria-label="Target column">
                        <SelectValue placeholder="Choose a column" />
                      </SelectTrigger>
                      <SelectContent>
                        {s.columns.map((c) => (
                          <SelectItem key={c.name} value={c.name} description={`${c.dtype}${c.distinct !== null ? ` · ${formatNumber(c.distinct)} distinct` : ""}${c.is_pii ? " · PII" : ""}`}>
                            {c.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </Field>
                )}
                {s.taskType === "forecast" && (
                  <Field id="ml-time" label="Time column" hint="Forecasts use a time-ordered split; the series is aggregated by this column.">
                    <Select value={s.timeColumn ?? undefined} onValueChange={(v) => dispatch({ type: "set_time_column", column: v })}>
                      <SelectTrigger aria-label="Time column">
                        <SelectValue placeholder="Choose a date/time column" />
                      </SelectTrigger>
                      <SelectContent>
                        {s.columns
                          .filter((c) => isTemporal(c.dtype) && c.name !== s.target)
                          .map((c) => (
                            <SelectItem key={c.name} value={c.name} description={c.dtype}>
                              {c.name}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                  </Field>
                )}
              </div>
            )}
            {s.step === "features" && (
              <FeaturesStep
                columns={s.columns ?? []}
                state={s}
                previewError={preview.isError && !isUnavailableEndpoint(preview.error) ? preview.error : null}
                onRetry={() => void preview.refetch()}
                onToggle={(c) => dispatch({ type: "toggle_feature", column: c })}
                onSetAll={(cols) => dispatch({ type: "set_features", columns: cols })}
              />
            )}
            {s.step === "review" && (
              <div className="space-y-5">
                <Field id="ml-name" label="Project name">
                  <Input value={s.name} onChange={(e) => dispatch({ type: "set_name", name: e.target.value })} maxLength={120} />
                </Field>
                <Field id="ml-desc" label="Business goal (optional)" hint="Mentioning compliance or credit makes the recommender prefer explainable models.">
                  <Textarea value={s.description} onChange={(e) => dispatch({ type: "set_description", description: e.target.value })} maxLength={2000} />
                </Field>
                <dl className="grid gap-3 rounded-card border border-border bg-surface-2/50 p-4 text-sm sm:grid-cols-2">
                  <Summary label="Dataset" value={s.datasetName} />
                  <Summary label="Task" value={s.taskType ? TASK_LABELS[s.taskType] : null} />
                  <Summary label="Target" value={s.target} mono />
                  {s.taskType === "forecast" && <Summary label="Time column" value={s.timeColumn} mono />}
                  <Summary label="Features" value={`${s.features.length} selected`} />
                  <Summary label="Excluded" value={`${(s.preview?.excluded_by_policy.length ?? 0) + (s.preview?.leakage_dropped.length ?? 0)} with reasons`} />
                </dl>
              </div>
            )}
          </motion.div>
        </AnimatePresence>
        <div className="flex items-center justify-between gap-3 border-t border-border bg-surface-2/60 px-6 py-3.5">
          <Button variant="ghost" onClick={() => dispatch({ type: "back" })} disabled={idx === 0}>
            <ArrowLeft /> Back
          </Button>
          <div className="flex items-center gap-3">
            {err && s.step !== "dataset" && <span className="hidden text-xs text-fg-muted sm:inline">{err}</span>}
            {s.step === "review" ? (
              <Button onClick={() => void submit()} disabled={!!err} loading={create.isPending}>
                <Check /> Create project
              </Button>
            ) : (
              <Button onClick={() => dispatch({ type: "next" })} disabled={!!err}>
                Continue <ArrowRight />
              </Button>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}

function Summary({ label, value, mono }: { label: string; value: string | null | undefined; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className={cn("truncate font-medium text-fg", mono && "font-mono text-[13px]")}>{value || "—"}</dd>
    </div>
  );
}

function DatasetStep({
  loading,
  error,
  datasets,
  selected,
  columnsLoading,
  columnsError,
  onSelect,
}: {
  loading: boolean;
  error: unknown;
  datasets: Array<{ id: string; name: string; row_count: number | null; column_count: number | null; classification: string }>;
  selected: string | null;
  columnsLoading: boolean;
  columnsError: unknown;
  onSelect: (d: { id: string; name: string; row_count: number | null }) => void;
}) {
  const { slug } = useOrgContext();
  if (error) return <ErrorState compact error={error} />;
  if (loading)
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-20 rounded-card" />
        ))}
      </div>
    );
  if (datasets.length === 0)
    return (
      <EmptyState
        compact
        icon={<Database />}
        title="No ready datasets"
        description="Models train on profiled datasets. Upload or import one first."
        action={
          <Button asChild variant="secondary">
            <Link href={`/o/${slug}/data/datasets`}>Open datasets</Link>
          </Button>
        }
      />
    );
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Dataset">
        {datasets.map((d) => (
          <button
            key={d.id}
            type="button"
            role="radio"
            aria-checked={selected === d.id}
            onClick={() => onSelect(d)}
            className={cn("flex items-start gap-3 rounded-card border p-3 text-left transition-colors focus-ring", selected === d.id ? "border-accent bg-accent-soft/60" : "border-border hover:border-border-strong")}
          >
            <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-control bg-surface-2 text-fg-muted">
              <Database className="size-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2">
                <span className="truncate text-sm font-medium text-fg">{d.name}</span>
                <ClassificationBadge value={d.classification} className="ml-auto" />
              </span>
              <span className="mt-0.5 block text-xs text-fg-muted">
                {formatNumber(d.row_count)} rows · {formatNumber(d.column_count)} columns
              </span>
            </span>
          </button>
        ))}
      </div>
      {columnsLoading && <p className="text-xs text-fg-muted">Loading columns…</p>}
      {!!columnsError && <ErrorState compact error={columnsError} />}
    </div>
  );
}

function ReasonList({ title, icon, items, tone }: { title: string; icon: React.ReactNode; items: ColumnReason[]; tone: "warning" | "danger" }) {
  if (!items.length) return null;
  return (
    <div className={cn("rounded-card border p-3", tone === "danger" ? "border-danger/30 bg-danger-soft/40" : "border-warning/30 bg-warning-soft/40")}>
      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-fg [&_svg]:size-4">
        {icon}
        {title} <Badge tone={tone}>{items.length}</Badge>
      </div>
      <ul className="space-y-1 text-xs">
        {items.map((r) => (
          <li key={r.column} className="flex gap-2">
            <span className="shrink-0 font-mono text-fg">{r.column}</span>
            <span className="text-fg-muted">{r.reason || "Excluded."}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function FeaturesStep({
  columns,
  state,
  previewError,
  onRetry,
  onToggle,
  onSetAll,
}: {
  columns: Array<{ name: string; dtype: string }>;
  state: ReturnType<typeof wizardReducer>;
  previewError: unknown;
  onRetry: () => void;
  onToggle: (c: string) => void;
  onSetAll: (cols: string[]) => void;
}) {
  if (previewError) return <ErrorState compact error={previewError} onRetry={onRetry} />;
  const p = state.preview;
  if (!p)
    return (
      <div className="space-y-2" aria-busy>
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-8" />
        ))}
      </div>
    );
  const blocked = blockedColumns(state);
  const dtype = new Map(columns.map((c) => [c.name, c.dtype]));
  const selectable = p.candidates.filter((c) => !blocked.has(c));
  return (
    <div className="space-y-4">
      {!p.server && (
        <p className="flex items-start gap-2 rounded-control bg-info-soft px-3 py-2 text-xs text-info">
          <Info className="mt-px size-3.5 shrink-0" />
          This server has no feature-preview endpoint yet, so identifier-like columns were flagged locally. Your data policy (masked or denied
          columns) is applied by the server when the project is created; the project page lists anything it excluded.
        </p>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        <ReasonList title="Excluded by data policy" icon={<ShieldOff className="text-danger" />} items={p.excluded_by_policy} tone="danger" />
        <ReasonList title="Dropped to prevent leakage" icon={<Unplug className="text-warning" />} items={p.leakage_dropped} tone="warning" />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-sm text-fg">
          <span className="tabular font-semibold">{state.features.length}</span> of {selectable.length} features selected
        </span>
        <div className="flex gap-1">
          <Button size="xs" variant="ghost" onClick={() => onSetAll(selectable)}>
            Select all
          </Button>
          <Button size="xs" variant="ghost" onClick={() => onSetAll([])}>
            Clear
          </Button>
        </div>
      </div>
      <ul className="grid gap-1.5 sm:grid-cols-2" aria-label="Features">
        {selectable.map((c) => {
          const id = `feat-${c}`;
          return (
            <li key={c}>
              <label htmlFor={id} className="flex cursor-pointer items-center gap-2.5 rounded-control border border-border px-3 py-2 text-sm hover:bg-surface-2">
                <Checkbox id={id} checked={state.features.includes(c)} onCheckedChange={() => onToggle(c)} />
                <span className="truncate font-mono text-[13px] text-fg">{c}</span>
                <span className="ml-auto shrink-0 text-xs text-fg-subtle">{dtype.get(c) ?? ""}</span>
              </label>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
