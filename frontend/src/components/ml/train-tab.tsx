"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ChevronDown, Cpu, FlaskConical, Lock, Play } from "lucide-react";
import { toast } from "sonner";
import { JobProgress } from "@/components/jobs/job-progress";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { errorMessage } from "@/lib/errors";
import { algorithmSpec, algorithmsFor, canonicalAlgorithm, defaultParams, validateParams, type AlgorithmSpec, type ParamSpec } from "@/lib/ml/algorithms";
import type { MlProject, Recommendation } from "@/lib/ml/types";
import { mlKeys, useStartExperiment } from "@/lib/queries/ml";
import { useQueryClient } from "@tanstack/react-query";
import { cn } from "@/lib/utils";

interface Selection {
  spec: AlgorithmSpec;
  values: Record<string, unknown>;
  fromRecommendation: boolean;
}

interface StartedJob {
  key: string;
  label: string;
  jobId: string | null;
  done: boolean;
}

export function TrainTab({
  project,
  preset,
  onPresetConsumed,
  onShowExperiments,
}: {
  project: MlProject;
  preset: Recommendation[] | null;
  onPresetConsumed: () => void;
  onShowExperiments: () => void;
}) {
  const { slug, can } = useOrgContext();
  const reduce = useReducedMotion();
  const available = React.useMemo(() => algorithmsFor(project.task_type), [project.task_type]);
  const [selected, setSelected] = React.useState<Record<string, Selection>>({});
  const [seed, setSeed] = React.useState("42");
  const [testSize, setTestSize] = React.useState("0.2");
  const [folds, setFolds] = React.useState("5");
  const [errors, setErrors] = React.useState<Record<string, Record<string, string>>>({});
  const [jobs, setJobs] = React.useState<StartedJob[]>([]);
  const [busy, setBusy] = React.useState(false);
  const start = useStartExperiment(slug, project.id);
  const qc = useQueryClient();

  // Presets from the Recommendations tab: select those algorithms with their recommended parameters.
  React.useEffect(() => {
    if (!preset) return;
    const next: Record<string, Selection> = {};
    for (const r of preset) {
      const spec = algorithmSpec(r.algorithm);
      if (spec && spec.tasks.includes(project.task_type)) next[spec.key] = { spec, values: defaultParams(spec, r.params), fromRecommendation: true };
    }
    if (Object.keys(next).length) setSelected(next);
    else toast.info("The recommended algorithms aren't in this form's catalog; pick them manually.");
    onPresetConsumed();
  }, [preset, project.task_type, onPresetConsumed]);

  if (!can("ml.manage")) {
    return (
      <Card>
        <EmptyState icon={<Lock />} title="Training needs ml.manage" description="You can view experiments and models, but not start training. Ask an owner or admin." />
      </Card>
    );
  }

  const toggle = (spec: AlgorithmSpec) =>
    setSelected((s) => {
      const copy = { ...s };
      if (copy[spec.key]) delete copy[spec.key];
      else copy[spec.key] = { spec, values: defaultParams(spec), fromRecommendation: false };
      return copy;
    });

  const setParam = (key: string, param: string, value: unknown) =>
    setSelected((s) => ({ ...s, [key]: { ...s[key], values: { ...s[key].values, [param]: value } } }));

  const seedN = Number(seed);
  const testN = Number(testSize);
  const foldsN = Number(folds);
  const globalErrors: Record<string, string> = {};
  if (!Number.isInteger(seedN) || seedN < 0 || seedN > 2 ** 31 - 1) globalErrors.seed = "A whole number from 0.";
  if (project.task_type !== "clustering" && (!Number.isFinite(testN) || testN < 0.05 || testN > 0.5)) globalErrors.test = "Between 0.05 and 0.5.";
  if (project.task_type !== "clustering" && (!Number.isInteger(foldsN) || foldsN < 2 || foldsN > 10)) globalErrors.folds = "2 to 10.";

  const submit = async () => {
    const entries = Object.values(selected);
    const all: Record<string, Record<string, string>> = {};
    const bodies = entries.map((sel) => {
      const v = validateParams(sel.spec, sel.values);
      if (Object.keys(v.errors).length) all[sel.spec.key] = v.errors;
      return { sel, values: v.values };
    });
    setErrors(all);
    if (Object.keys(all).length || Object.keys(globalErrors).length) return;
    setBusy(true);
    try {
      for (const { sel, values } of bodies) {
        try {
          const res = await start.mutateAsync({
            algorithm: sel.spec.key,
            hyperparameters: values,
            random_state: seedN,
            ...(project.task_type !== "clustering" ? { test_size: testN, cv_folds: foldsN } : {}),
          });
          setJobs((j) => [{ key: `${sel.spec.key}-${Date.now()}`, label: sel.spec.label, jobId: res.job_id, done: !res.job_id }, ...j].slice(0, 8));
        } catch (e) {
          toast.error(`${sel.spec.label}: ${errorMessage(e)}`);
        }
      }
    } finally {
      setBusy(false);
    }
  };

  const count = Object.keys(selected).length;

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="space-y-4">
        <Card>
          <CardHeader title="Algorithms" description="Pick one or more. Each becomes an experiment trained with cross-validation and a held-out test set." icon={<Cpu />} />
          <CardContent className="space-y-3">
            {available.map((spec) => {
              const sel = selected[spec.key];
              return (
                <div key={spec.key} className={cn("rounded-card border transition-colors", sel ? "border-accent/60 bg-accent-soft/30" : "border-border")}>
                  <label className="flex cursor-pointer items-start gap-3 p-3">
                    <Checkbox checked={!!sel} onCheckedChange={() => toggle(spec)} aria-label={spec.label} className="mt-0.5" />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2 text-sm font-medium text-fg">
                        {spec.label}
                        {sel?.fromRecommendation && <Badge tone="accent">Recommended</Badge>}
                      </span>
                      <span className="block text-xs text-fg-muted">{spec.description}</span>
                    </span>
                  </label>
                  <AnimatePresence initial={false}>
                    {sel && spec.params.length > 0 && (
                      <motion.div initial={reduce ? false : { height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={reduce ? undefined : { height: 0, opacity: 0 }} className="overflow-hidden">
                        <ParamsForm spec={spec} values={sel.values} errors={errors[spec.key] ?? {}} onChange={(k, v) => setParam(spec.key, k, v)} />
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              );
            })}
          </CardContent>
        </Card>
      </div>

      <div className="space-y-4">
        <Card className="xl:sticky xl:top-4">
          <CardHeader title="Run settings" description="Recorded on every experiment so results reproduce." />
          <CardContent className="space-y-3">
            <Field id="ml-seed" label="Random state" error={globalErrors.seed}>
              <Input inputMode="numeric" value={seed} onChange={(e) => setSeed(e.target.value)} />
            </Field>
            {project.task_type !== "clustering" && (
              <div className="grid grid-cols-2 gap-3">
                <Field id="ml-test" label="Test fraction" error={globalErrors.test}>
                  <Input inputMode="decimal" value={testSize} onChange={(e) => setTestSize(e.target.value)} />
                </Field>
                <Field id="ml-folds" label="CV folds" error={globalErrors.folds}>
                  <Input inputMode="numeric" value={folds} onChange={(e) => setFolds(e.target.value)} />
                </Field>
              </div>
            )}
            <p className="text-xs text-fg-muted">
              {project.task_type === "forecast"
                ? "Forecasts use a time-ordered split, never a random one."
                : project.task_type === "classification"
                  ? "The split is stratified by the target."
                  : "Large datasets are sampled to the server's training row cap; the sample is recorded on the experiment."}
            </p>
            <Button className="w-full" onClick={() => void submit()} disabled={count === 0 || busy} loading={busy}>
              <Play /> Train {count > 0 ? `${count} model${count === 1 ? "" : "s"}` : ""}
            </Button>
          </CardContent>
        </Card>

        {jobs.length > 0 && (
          <Card>
            <CardHeader
              title="Training jobs"
              icon={<FlaskConical />}
              actions={
                <Button size="xs" variant="ghost" onClick={onShowExperiments}>
                  View experiments
                </Button>
              }
            />
            <CardContent className="space-y-4">
              {jobs.map((j) =>
                j.jobId ? (
                  <JobProgress key={j.key} org={slug} jobId={j.jobId} title={j.label} showLogs={false} onDone={() => {
                    setJobs((all) => all.map((x) => (x.key === j.key ? { ...x, done: true } : x)));
                    void qc.invalidateQueries({ queryKey: mlKeys.experiments(slug, project.id), exact: true });
                  }} />
                ) : (
                  <p key={j.key} className="text-sm text-fg-muted">
                    {j.label}: queued.
                  </p>
                ),
              )}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function ParamsForm({ spec, values, errors, onChange }: { spec: AlgorithmSpec; values: Record<string, unknown>; errors: Record<string, string>; onChange: (key: string, v: unknown) => void }) {
  const [advanced, setAdvanced] = React.useState(false);
  const basic = spec.params.filter((p) => !p.advanced);
  const adv = spec.params.filter((p) => p.advanced);
  const hasAdvError = adv.some((p) => errors[p.key]);
  return (
    <div className="space-y-3 border-t border-border/70 px-3 pt-3 pb-3">
      <div className="grid gap-3 sm:grid-cols-2">
        {basic.map((p) => (
          <ParamInput key={p.key} algo={spec.key} p={p} value={values[p.key]} error={errors[p.key]} onChange={(v) => onChange(p.key, v)} />
        ))}
      </div>
      {adv.length > 0 && (
        <div>
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs font-medium text-fg-muted hover:text-fg focus-ring rounded-[4px]"
            aria-expanded={advanced || hasAdvError}
            onClick={() => setAdvanced((a) => !a)}
          >
            <ChevronDown className={cn("size-3.5 transition-transform", (advanced || hasAdvError) && "rotate-180")} /> Advanced
          </button>
          {(advanced || hasAdvError) && (
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {adv.map((p) => (
                <ParamInput key={p.key} algo={spec.key} p={p} value={values[p.key]} error={errors[p.key]} onChange={(v) => onChange(p.key, v)} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ParamInput({ algo, p, value, error, onChange }: { algo: string; p: ParamSpec; value: unknown; error?: string; onChange: (v: unknown) => void }) {
  const id = `${canonicalAlgorithm(algo)}-${p.key}`;
  if (p.kind === "select") {
    return (
      <Field id={id} label={p.label} hint={p.help} error={error}>
        <Select value={String(value ?? p.default)} onValueChange={onChange}>
          <SelectTrigger aria-label={p.label}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {p.options?.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
    );
  }
  return (
    <Field id={id} label={p.label} hint={p.help ?? (p.min !== undefined && p.max !== undefined ? `${p.min} – ${p.max}` : undefined)} error={error}>
      <Input inputMode={p.kind === "int" ? "numeric" : "decimal"} value={value === null || value === undefined ? "" : String(value)} placeholder={p.nullable ? "No limit" : undefined} onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}
