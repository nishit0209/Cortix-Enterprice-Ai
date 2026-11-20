"use client";

import * as React from "react";
import Link from "next/link";
import { motion, useReducedMotion } from "framer-motion";
import { Award, BookmarkPlus, FlaskConical, GitCompare, Play, Timer, X } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { CHART_COLORS } from "@/components/ui/chart-card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status";
import { errorMessage } from "@/lib/errors";
import { algorithmLabel } from "@/lib/ml/algorithms";
import { bestIndex, defaultPrimaryMetric, lowerIsBetter, metricBars, metricValue } from "@/lib/ml/mappers";
import type { Experiment, MlProject } from "@/lib/ml/types";
import { useCompareExperiments, useMlExperiment, useMlExperiments, useRegisterExperiment } from "@/lib/queries/ml";
import { cn, formatRelative } from "@/lib/utils";
import { MlError } from "./common";
import { ConfusionHeatmap, CurveChart, HBarChart, MetricBarsChart, ResidualScatter, fmtMetric } from "./ml-charts";

const MAX_COMPARE = 4;

export function ExperimentsTab({ project, onTrain }: { project: MlProject; onTrain: () => void }) {
  const { slug, can } = useOrgContext();
  const list = useMlExperiments(slug, project.id);
  const [picked, setPicked] = React.useState<string[]>([]);
  const [registering, setRegistering] = React.useState<Experiment | null>(null);
  const primary = project.primary_metric ?? defaultPrimaryMetric(project.task_type);

  const exps = React.useMemo(() => list.data ?? [], [list.data]);
  const succeeded = exps.filter((e) => e.status === "succeeded");
  const best = bestIndex(succeeded.map((e) => e.metrics.primary), primary);
  const bestId = best >= 0 ? succeeded[best].id : null;
  const { metricCols, primaryKey } = React.useMemo(() => {
    const keys = Array.from(new Set(exps.flatMap((e) => Object.keys(e.metrics.primary))));
    const pk = keys.find((k) => k === primary) ?? keys.find((k) => metricValue({ [k]: 1 }, primary) !== null) ?? null;
    return { metricCols: [...(pk ? [pk] : []), ...keys.filter((k) => k !== pk)].slice(0, 5), primaryKey: pk };
  }, [exps, primary]);

  const toggle = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length >= MAX_COMPARE ? p : [...p, id]));

  if (list.isError)
    return (
      <Card>
        <MlError error={list.error} what="Experiments" onRetry={() => void list.refetch()} />
      </Card>
    );
  if (list.isLoading) return <Skeleton className="h-64 rounded-card" />;
  if (!exps.length)
    return (
      <Card>
        <EmptyState
          icon={<FlaskConical />}
          title="No experiments yet"
          description="Train one or more algorithms to compare them here."
          action={
            can("ml.manage") ? (
              <Button onClick={onTrain}>
                <Play /> Train models
              </Button>
            ) : undefined
          }
        />
      </Card>
    );

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader
          title="Experiments"
          description={`Ranked on ${primary} (${lowerIsBetter(primary) ? "lower" : "higher"} is better). Select 2–${MAX_COMPARE} to compare, or one to inspect.`}
          actions={
            picked.length > 0 && (
              <Button size="xs" variant="ghost" onClick={() => setPicked([])}>
                <X /> Clear selection
              </Button>
            )
          }
        />
        <div className="scrollbar-thin overflow-x-auto px-5 pb-4">
          <table className="w-full min-w-[720px] text-sm">
            <caption className="sr-only">Experiments</caption>
            <thead className="text-xs text-fg-muted">
              <tr className="border-b border-border">
                <th className="w-8 py-2" />
                <th className="py-2 pr-3 text-left font-medium">Algorithm</th>
                <th className="py-2 pr-3 text-left font-medium">Status</th>
                {metricCols.map((m) => (
                  <th key={m} className={cn("py-2 pr-3 text-right font-medium", m === primaryKey && "text-fg")}>
                    {m}
                  </th>
                ))}
                <th className="py-2 pr-3 text-right font-medium">Duration</th>
                <th className="py-2 pr-3 text-right font-medium">Started</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {exps.map((e) => {
                const isPicked = picked.includes(e.id);
                const color = isPicked ? CHART_COLORS[picked.indexOf(e.id)] : undefined;
                return (
                  <tr key={e.id} className={cn("border-b border-border/60 last:border-0", isPicked && "bg-surface-2/60")}>
                    <td className="py-2">
                      <Checkbox
                        checked={isPicked}
                        disabled={!isPicked && picked.length >= MAX_COMPARE}
                        onCheckedChange={() => toggle(e.id)}
                        aria-label={`Select ${algorithmLabel(e.algorithm)} experiment`}
                      />
                    </td>
                    <td className="py-2 pr-3">
                      <span className="flex items-center gap-2">
                        {color && <span className="size-2.5 rounded-full" style={{ background: color }} aria-hidden />}
                        <span className="font-medium text-fg">{algorithmLabel(e.algorithm)}</span>
                        {e.id === bestId && (
                          <Badge tone="success">
                            <Award /> Best
                          </Badge>
                        )}
                      </span>
                      {e.error && <span className="mt-0.5 block max-w-xs truncate text-xs text-danger" title={e.error}>{e.error}</span>}
                    </td>
                    <td className="py-2 pr-3">
                      <StatusBadge status={String(e.status)} />
                    </td>
                    {metricCols.map((m) => (
                      <td key={m} className={cn("tabular py-2 pr-3 text-right", m === primaryKey ? "font-semibold text-fg" : "text-fg-muted")}>
                        {fmtMetric(e.metrics.primary[m])}
                        {e.metrics.cv_std[m] !== undefined && <span className="ml-1 text-[10px] text-fg-subtle">±{fmtMetric(e.metrics.cv_std[m])}</span>}
                      </td>
                    ))}
                    <td className="tabular py-2 pr-3 text-right text-fg-muted">{e.duration_s === null ? "—" : `${e.duration_s.toFixed(1)} s`}</td>
                    <td className="py-2 pr-3 text-right text-xs text-fg-muted">{formatRelative(e.created_at)}</td>
                    <td className="py-2 text-right">
                      {e.status === "succeeded" && can("ml.manage") && (
                        <Button size="xs" variant="ghost" onClick={() => setRegistering(e)}>
                          <BookmarkPlus /> Register
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {picked.length === 1 && <SingleExperiment id={picked[0]} />}
      {picked.length >= 2 && <Comparison ids={picked} primary={primary} />}

      <RegisterDialog experiment={registering} project={project} onClose={() => setRegistering(null)} />
    </div>
  );
}

function SingleExperiment({ id }: { id: string }) {
  const { slug } = useOrgContext();
  const q = useMlExperiment(slug, id);
  if (q.isError) return <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return <Skeleton className="h-64 rounded-card" />;
  return <ComparePanels experiments={[q.data]} primary={null} />;
}

function Comparison({ ids, primary }: { ids: string[]; primary: string }) {
  const { slug } = useOrgContext();
  const q = useCompareExperiments(slug, ids);
  if (q.isError) return <MlError compact error={q.error} what="Comparison" onRetry={() => void q.refetch()} />;
  if (!q.data) return <Skeleton className="h-64 rounded-card" />;
  return <ComparePanels experiments={q.data.experiments} primary={q.data.primary_metric ?? primary} ids={ids} />;
}

function ComparePanels({ experiments, primary, ids }: { experiments: Experiment[]; primary: string | null; ids?: string[] }) {
  const reduce = useReducedMotion();
  const order = ids ?? experiments.map((e) => e.id);
  const series = experiments.map((e) => ({ id: e.id, label: algorithmLabel(e.algorithm), color: CHART_COLORS[Math.max(0, order.indexOf(e.id)) % CHART_COLORS.length] }));
  const bars = metricBars(experiments.map((e) => ({ id: e.id, metrics: e.metrics.primary })));
  // Keep the bar chart on comparable 0..1 metrics; error metrics with other scales go to the table.
  const unitBars = bars.filter((r) => experiments.every((e) => r[e.id] === null || (Number(r[e.id]) >= 0 && Number(r[e.id]) <= 1)));
  const roc = experiments.filter((e) => e.roc);
  const pr = experiments.filter((e) => e.pr);
  const withConfusion = experiments.filter((e) => e.confusion);
  const withResiduals = experiments.filter((e) => e.residuals.length > 0);
  const withImportance = experiments.filter((e) => e.importance.length > 0);
  const withShap = experiments.filter((e) => e.shap.length > 0);
  const colorOf = (id: string) => series.find((s) => s.id === id)?.color ?? CHART_COLORS[0];

  return (
    <motion.div initial={reduce ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="space-y-5">
      <div className="flex items-center gap-2 text-sm font-semibold text-fg">
        <GitCompare className="size-4 text-accent" /> {experiments.length > 1 ? `Comparing ${experiments.length} experiments` : `${algorithmLabel(experiments[0].algorithm)} details`}
      </div>

      <Card>
        <CardHeader title="Metrics" description={primary ? `Primary metric: ${primary}. Test split; ± is the cross-validation spread.` : "Test split; ± is the cross-validation spread."} />
        <CardContent className="space-y-4">
          {unitBars.length > 0 && <MetricBarsChart rows={unitBars} series={series} />}
          <div className="scrollbar-thin overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-fg-muted">
                <tr className="border-b border-border">
                  <th className="py-2 pr-3 text-left font-medium">Metric</th>
                  {series.map((s) => (
                    <th key={s.id} className="py-2 pr-3 text-right font-medium">
                      <span className="inline-flex items-center gap-1.5">
                        <span className="size-2 rounded-full" style={{ background: s.color }} aria-hidden />
                        {s.label}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bars.map((r) => {
                  const name = String(r.metric);
                  const b = bestIndex(experiments.map((e) => e.metrics.primary), name);
                  return (
                    <tr key={name} className="border-b border-border/60 last:border-0">
                      <td className={cn("py-1.5 pr-3 font-mono text-xs", name === primary ? "text-fg" : "text-fg-muted")}>{name}</td>
                      {experiments.map((e, i) => (
                        <td key={e.id} className={cn("tabular py-1.5 pr-3 text-right", experiments.length > 1 && i === b ? "font-semibold text-success" : "text-fg")}>
                          {fmtMetric(r[e.id])}
                        </td>
                      ))}
                    </tr>
                  );
                })}
                <tr>
                  <td className="py-1.5 pr-3 font-mono text-xs text-fg-muted">random_state</td>
                  {experiments.map((e) => (
                    <td key={e.id} className="tabular py-1.5 pr-3 text-right text-fg-muted">
                      {e.random_state ?? "—"}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>
          {experiments.some((e) => e.sampled) && (
            <p className="flex items-center gap-1.5 text-xs text-fg-muted">
              <Timer className="size-3.5" />
              {experiments
                .filter((e) => e.sampled)
                .map((e) => `${algorithmLabel(e.algorithm)} trained on a stratified sample of ${e.sampled!.rows.toLocaleString()} of ${e.sampled!.of.toLocaleString()} rows.`)
                .join(" ")}
            </p>
          )}
        </CardContent>
      </Card>

      {(roc.length > 0 || pr.length > 0) && (
        <div className="grid gap-5 lg:grid-cols-2">
          {roc.length > 0 && (
            <Card>
              <CardHeader title="ROC curve" description={roc.map((e) => `${algorithmLabel(e.algorithm)} AUC ${fmtMetric(e.roc!.auc ?? metricValue(e.metrics.primary, "roc_auc"))}`).join(" · ")} />
              <CardContent>
                <CurveChart kind="roc" series={roc.map((e) => ({ id: e.id, label: algorithmLabel(e.algorithm), color: colorOf(e.id), points: e.roc!.points }))} />
              </CardContent>
            </Card>
          )}
          {pr.length > 0 && (
            <Card>
              <CardHeader title="Precision–recall curve" description={pr.map((e) => `${algorithmLabel(e.algorithm)} AP ${fmtMetric(e.pr!.auc ?? metricValue(e.metrics.primary, "pr_auc"))}`).join(" · ")} />
              <CardContent>
                <CurveChart kind="pr" series={pr.map((e) => ({ id: e.id, label: algorithmLabel(e.algorithm), color: colorOf(e.id), points: e.pr!.points }))} />
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {withConfusion.length > 0 && (
        <div className={cn("grid gap-5", withConfusion.length > 1 && "lg:grid-cols-2")}>
          {withConfusion.map((e) => (
            <Card key={e.id}>
              <CardHeader title={`Confusion matrix · ${algorithmLabel(e.algorithm)}`} />
              <CardContent>
                <ConfusionHeatmap cm={e.confusion!} />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {withResiduals.length > 0 && (
        <div className={cn("grid gap-5", withResiduals.length > 1 && "lg:grid-cols-2")}>
          {withResiduals.map((e) => (
            <Card key={e.id}>
              <CardHeader title={`Residuals · ${algorithmLabel(e.algorithm)}`} description="Actual minus predicted on the test split. A good model scatters evenly around zero." />
              <CardContent>
                <ResidualScatter points={e.residuals} color={colorOf(e.id)} />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {(withImportance.length > 0 || withShap.length > 0) && (
        <div className="grid gap-5 lg:grid-cols-2">
          {withImportance.map((e) => (
            <Card key={`imp-${e.id}`}>
              <CardHeader title={`Feature importance · ${algorithmLabel(e.algorithm)}`} description="Model-native or permutation importance." />
              <CardContent>
                <HBarChart items={e.importance.slice(0, 15)} color={colorOf(e.id)} valueLabel="Importance" />
              </CardContent>
            </Card>
          ))}
          {withShap.map((e) => (
            <Card key={`shap-${e.id}`}>
              <CardHeader title={`SHAP summary · ${algorithmLabel(e.algorithm)}`} description="Mean |SHAP value| per feature over a bounded sample." />
              <CardContent>
                <HBarChart items={e.shap.slice(0, 15)} color={colorOf(e.id)} valueLabel="Mean |SHAP|" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </motion.div>
  );
}

function RegisterDialog({ experiment, project, onClose }: { experiment: Experiment | null; project: MlProject; onClose: () => void }) {
  const { slug } = useOrgContext();
  const register = useRegisterExperiment(slug);
  const [name, setName] = React.useState("");
  const [done, setDone] = React.useState<{ model_id: string | null; version: number | null } | null>(null);
  React.useEffect(() => {
    if (experiment) {
      setName(project.name);
      setDone(null);
    }
  }, [experiment, project.name]);
  return (
    <Dialog open={!!experiment} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="sm">
        <DialogHeader
          title="Register model version"
          description={experiment ? `Stores the full ${algorithmLabel(experiment.algorithm)} pipeline (preprocessing included) with a checksum. New versions start unpromoted.` : undefined}
          icon={<BookmarkPlus />}
        />
        <DialogBody className="space-y-3">
          {done ? (
            <div className="space-y-2 text-sm">
              <p className="text-fg">Registered{done.version ? ` as version ${done.version}` : ""}.</p>
              {done.model_id && (
                <Link href={`/o/${slug}/ml/models/${done.model_id}`} className="font-medium text-accent hover:underline">
                  Open in the model registry
                </Link>
              )}
            </div>
          ) : (
            <Field id="reg-name" label="Model name" hint="Registering under an existing name adds a new version.">
              <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
            </Field>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            {done ? "Close" : "Cancel"}
          </Button>
          {!done && (
            <Button
              loading={register.isPending}
              disabled={!name.trim()}
              onClick={async () => {
                if (!experiment) return;
                try {
                  setDone(await register.mutateAsync({ id: experiment.id, name: name.trim() }));
                  toast.success("Model registered");
                } catch (e) {
                  toast.error(errorMessage(e));
                }
              }}
            >
              Register
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
