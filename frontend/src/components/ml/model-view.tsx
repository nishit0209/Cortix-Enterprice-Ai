"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { motion, useReducedMotion } from "framer-motion";
import { Activity, ArrowLeft, Boxes, Check, Code2, Copy, Database, KeyRound, Layers, Play, Rocket, ShieldCheck, Sparkles, TriangleAlert, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { JobProgress } from "@/components/jobs/job-progress";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ChartCard } from "@/components/ui/chart-card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState, ErrorState, Progress, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { StatCard } from "@/components/ui/stat-card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, isApiError, unwrapList } from "@/lib/api";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { algorithmLabel } from "@/lib/ml/algorithms";
import { PSI_MODERATE, PSI_STABLE, PSI_TONE, servingVersion } from "@/lib/ml/mappers";
import { curlExample, initialFormValues, inputTypeFor, serverFieldErrors, validatePredictionForm, type FormValues } from "@/lib/ml/predict-form";
import type { FeatureSpec, MlModel, ModelVersion, Prediction } from "@/lib/ml/types";
import { useDatasets } from "@/lib/queries/datasets";
import { useBatchPredict, useMlModel, useMlMonitoring, usePredict, usePromoteVersion } from "@/lib/queries/ml";
import type { Paginated } from "@/lib/types";
import { cn, formatDateTime, formatNumber, formatRelative } from "@/lib/utils";
import { MlError, StageBadge, TaskBadge } from "./common";
import { ContributionBars, DistributionCompare, PsiBars, fmtMetric } from "./ml-charts";

export function ModelView() {
  const { id } = useParams<{ id: string }>();
  const { slug } = useOrgContext();
  const model = useMlModel(slug, id);
  useSetBreadcrumb(model.data?.name);
  const [tab, setTab] = React.useState("versions");

  if (model.isError)
    return (
      <Card>
        <MlError error={model.error} what="This model" onRetry={() => void model.refetch()} />
      </Card>
    );
  const m = model.data;
  const serving = m ? servingVersion(m) : null;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/ml`} className="inline-flex items-center gap-1 hover:text-fg">
            <ArrowLeft className="size-3" /> ML Workspace
          </Link>
        }
        title={m ? m.name : <Skeleton className="h-6 w-48" />}
        badge={m && <TaskBadge task={m.task_type} />}
        description={
          m?.project_id ? (
            <>
              From project{" "}
              <Link href={`/o/${slug}/ml/projects/${m.project_id}`} className="font-medium text-fg hover:underline">
                {m.project_name ?? "project"}
              </Link>
              {m.target_column && (
                <>
                  , predicting <span className="font-mono">{m.target_column}</span>
                </>
              )}
              .
            </>
          ) : undefined
        }
      />
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Model summary">
        <StatCard label="Serving version" icon={<Rocket />} loading={!m} value={serving ? `v${serving.version}` : "—"} hint={serving ? <StageBadge stage={serving.stage} /> : "Nothing promoted yet"} accent="success" />
        <StatCard label="Versions" icon={<Layers />} loading={!m} value={m ? formatNumber(m.versions.length) : "—"} />
        <StatCard label="Algorithm" icon={<Boxes />} loading={!m} value={serving?.algorithm ? algorithmLabel(serving.algorithm) : "—"} accent="teal" />
        <StatCard label="Features" icon={<Database />} loading={!m} value={serving ? formatNumber(serving.feature_schema.length) : "—"} accent="accent" />
      </section>

      {m && (
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList aria-label="Model sections">
            <TabsTrigger value="versions">
              <Layers /> Versions
            </TabsTrigger>
            <TabsTrigger value="predict">
              <Wand2 /> Playground
            </TabsTrigger>
            <TabsTrigger value="api">
              <Code2 /> API
            </TabsTrigger>
            <TabsTrigger value="monitoring">
              <Activity /> Monitoring
            </TabsTrigger>
          </TabsList>
          <TabsContent value="versions">
            <VersionsTab model={m} />
          </TabsContent>
          <TabsContent value="predict">{serving ? <Playground model={m} version={serving} /> : <NoVersion />}</TabsContent>
          <TabsContent value="api">{serving ? <ApiPanel model={m} version={serving} /> : <NoVersion />}</TabsContent>
          <TabsContent value="monitoring">
            <MonitoringTab model={m} />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}

function NoVersion() {
  return (
    <Card>
      <EmptyState icon={<Layers />} title="No version to serve" description="Register an experiment to create the first version." />
    </Card>
  );
}

/* ---------------- versions + promotion ---------------- */

type Stage = "staging" | "production" | "archived";

function VersionsTab({ model }: { model: MlModel }) {
  const { slug, can } = useOrgContext();
  const promote = usePromoteVersion(slug, model.id);
  const [pending, setPending] = React.useState<{ v: ModelVersion; stage: Stage } | null>(null);
  const canPromote = can("ml.promote");
  const current = model.versions.find((v) => v.stage === "production");
  const metricKeys = Array.from(new Set(model.versions.flatMap((v) => Object.keys(v.metrics)))).slice(0, 4);

  return (
    <>
      <Card>
        <CardHeader
          title="Versions"
          description="One production version per model; promoting another archives the previous one. Every promotion is audited."
          actions={!canPromote && <Badge tone="outline">Promotion needs ml.promote</Badge>}
        />
        <div className="scrollbar-thin overflow-x-auto px-5 pb-4">
          <table className="w-full min-w-[640px] text-sm">
            <caption className="sr-only">Model versions</caption>
            <thead className="text-xs text-fg-muted">
              <tr className="border-b border-border">
                <th className="py-2 pr-3 text-left font-medium">Version</th>
                <th className="py-2 pr-3 text-left font-medium">Stage</th>
                <th className="py-2 pr-3 text-left font-medium">Algorithm</th>
                {metricKeys.map((k) => (
                  <th key={k} className="py-2 pr-3 text-right font-medium">
                    {k}
                  </th>
                ))}
                <th className="py-2 pr-3 text-left font-medium">Promoted</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {model.versions.map((v) => (
                <tr key={v.version} className="border-b border-border/60 last:border-0">
                  <td className="py-2 pr-3 font-mono text-xs">v{v.version}</td>
                  <td className="py-2 pr-3">
                    <StageBadge stage={v.stage} />
                  </td>
                  <td className="py-2 pr-3">{v.algorithm ? algorithmLabel(v.algorithm) : "—"}</td>
                  {metricKeys.map((k) => (
                    <td key={k} className="tabular py-2 pr-3 text-right">
                      {fmtMetric(v.metrics[k])}
                    </td>
                  ))}
                  <td className="py-2 pr-3 text-xs text-fg-muted">
                    {v.promoted_at ? `${formatRelative(v.promoted_at)}${v.promoted_by ? ` by ${v.promoted_by}` : ""}` : v.created_at ? `Registered ${formatRelative(v.created_at)}` : "—"}
                  </td>
                  <td className="py-2 text-right">
                    {canPromote && (
                      <div className="flex justify-end gap-1">
                        {v.stage !== "staging" && v.stage !== "production" && (
                          <Button size="xs" variant="ghost" onClick={() => setPending({ v, stage: "staging" })}>
                            Stage
                          </Button>
                        )}
                        {v.stage !== "production" && (
                          <Button size="xs" variant="secondary" onClick={() => setPending({ v, stage: "production" })}>
                            <Rocket /> Promote
                          </Button>
                        )}
                        {v.stage !== "archived" && (
                          <Button size="xs" variant="ghost" onClick={() => setPending({ v, stage: "archived" })}>
                            Archive
                          </Button>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {model.versions.length === 0 && <EmptyState compact icon={<Layers />} title="No versions yet" />}
        </div>
        {model.versions[0]?.artifact_sha256 && (
          <p className="flex items-center gap-1.5 border-t border-border px-5 py-2.5 text-xs text-fg-muted">
            <ShieldCheck className="size-3.5 text-success" /> Artifacts load only when their SHA-256 matches. Latest:{" "}
            <span className="font-mono">{model.versions[0].artifact_sha256.slice(0, 16)}…</span>
          </p>
        )}
      </Card>
      <ConfirmDialog
        open={!!pending}
        onOpenChange={(o) => !o && setPending(null)}
        tone={pending?.stage === "archived" ? "danger" : "default"}
        title={pending ? `${pending.stage === "production" ? "Promote" : pending.stage === "staging" ? "Stage" : "Archive"} version ${pending.v.version}?` : ""}
        description={
          pending?.stage === "production"
            ? current && current.version !== pending.v.version
              ? `Version ${current.version} is in production now and will be archived. Prediction API calls switch to version ${pending.v.version} immediately.`
              : "Prediction API calls will use this version."
            : pending?.stage === "archived"
              ? "Archived versions stop serving predictions."
              : "Staging versions can be tried in the playground before promotion."
        }
        confirmLabel={pending?.stage === "production" ? "Promote to production" : pending?.stage === "staging" ? "Move to staging" : "Archive"}
        loading={promote.isPending}
        onConfirm={async () => {
          if (!pending) return;
          try {
            const res = await promote.mutateAsync({ version: pending.v.version, stage: pending.stage });
            const approval = res && typeof res === "object" && "approval_id" in res;
            toast.success(approval ? "Approval requested. The version changes stage once it is approved." : `Version ${pending.v.version} is now ${pending.stage}`);
            setPending(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </>
  );
}

/* ---------------- playground ---------------- */

function Playground({ model, version }: { model: MlModel; version: ModelVersion }) {
  const { slug, can } = useOrgContext();
  const schema = version.feature_schema;
  const [values, setValues] = React.useState<FormValues>(() => initialFormValues(schema));
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const predict = usePredict(slug, model.id);
  const [result, setResult] = React.useState<{ p: Prediction; version: number | null } | null>(null);
  const reduce = useReducedMotion();

  React.useEffect(() => {
    setValues(initialFormValues(schema));
    setErrors({});
    setResult(null);
  }, [schema]);

  const run = async () => {
    const r = validatePredictionForm(schema, values);
    setErrors(r.errors);
    if (!r.row) return;
    try {
      const res = await predict.mutateAsync({ rows: [r.row], version: version.version, explain: true });
      if (!res.predictions[0]) throw new Error("The API returned no prediction.");
      setResult({ p: res.predictions[0], version: res.model_version ?? version.version });
    } catch (e) {
      if (isApiError(e) && e.status === 400) setErrors(serverFieldErrors(e.errors, schema));
      toast.error(errorMessage(e));
    }
  };

  if (!schema.length) {
    return (
      <Card>
        <EmptyState icon={<TriangleAlert />} title="No feature schema" description="This version has no feature schema, so the form can't be generated." />
      </Card>
    );
  }

  return (
    <div className="space-y-5">
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Card>
          <CardHeader title="Single prediction" description={`Version ${version.version} (${version.stage === "none" ? "registered" : version.stage}). Empty fields are imputed by the model's pipeline.`} icon={<Wand2 />} />
          <CardContent>
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                void run();
              }}
            >
              <div className="grid gap-3 sm:grid-cols-2">
                {schema.map((f) => (
                  <FeatureInput key={f.name} f={f} value={values[f.name] ?? ""} error={errors[f.name]} onChange={(v) => setValues((s) => ({ ...s, [f.name]: v }))} />
                ))}
              </div>
              <div className="flex gap-2">
                <Button type="submit" loading={predict.isPending}>
                  <Play /> Predict
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    setValues(initialFormValues(schema));
                    setErrors({});
                    setResult(null);
                  }}
                >
                  Reset
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
        <Card className="self-start" aria-live="polite">
          <CardHeader title="Result" icon={<Sparkles />} />
          <CardContent>
            {!result ? (
              <p className="text-sm text-fg-muted">Fill the form and run a prediction. The top contributions explain this single prediction (local SHAP).</p>
            ) : (
              <motion.div key={JSON.stringify(result.p.value)} initial={reduce ? false : { opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} className="space-y-4">
                <div>
                  <div className="text-xs text-fg-subtle">Prediction</div>
                  <div className="tabular text-3xl font-semibold tracking-[-0.02em] text-fg">{typeof result.p.value === "number" ? fmtMetric(result.p.value) : String(result.p.value ?? "—")}</div>
                  {result.version && <div className="text-xs text-fg-muted">from version {result.version}</div>}
                </div>
                {result.p.probability !== null && (
                  <div className="space-y-1">
                    <div className="flex justify-between text-xs">
                      <span className="text-fg-muted">Probability</span>
                      <span className="tabular font-medium text-fg">{(result.p.probability * 100).toFixed(1)}%</span>
                    </div>
                    <Progress value={result.p.probability * 100} label="Prediction probability" />
                  </div>
                )}
                {result.p.probabilities.length > 1 && (
                  <ul className="space-y-1 text-xs">
                    {result.p.probabilities.slice(0, 5).map((c) => (
                      <li key={c.label} className="flex justify-between">
                        <span className="text-fg-muted">{c.label}</span>
                        <span className="tabular text-fg">{(c.p * 100).toFixed(1)}%</span>
                      </li>
                    ))}
                  </ul>
                )}
                {result.p.contributions.length > 0 && (
                  <div className="space-y-2">
                    <div className="text-xs font-medium text-fg-muted">Top contributions</div>
                    <ContributionBars items={result.p.contributions.slice(0, 8)} />
                  </div>
                )}
              </motion.div>
            )}
          </CardContent>
        </Card>
      </div>
      {can("ml.manage") && <BatchPredict model={model} version={version} />}
    </div>
  );
}

function FeatureInput({ f, value, error, onChange }: { f: FeatureSpec; value: string; error?: string; onChange: (v: string) => void }) {
  const id = `feat-${f.name}`;
  const hint = f.dtype === "category" && f.categories.length ? `${f.categories.length} known values` : f.min !== null || f.max !== null ? `${f.min ?? "…"} – ${f.max ?? "…"}` : f.dtype;
  if (f.dtype === "boolean") {
    return (
      <Field id={id} label={f.name} error={error} hint="boolean">
        <Select value={value || undefined} onValueChange={onChange}>
          <SelectTrigger aria-label={f.name}>
            <SelectValue placeholder="Empty (imputed)" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="true">true</SelectItem>
            <SelectItem value="false">false</SelectItem>
          </SelectContent>
        </Select>
      </Field>
    );
  }
  const listId = f.dtype === "category" && f.categories.length ? `${id}-options` : undefined;
  return (
    <div>
      <Field id={id} label={f.name} error={error} hint={hint}>
        <Input type={inputTypeFor(f)} step="any" list={listId} value={value} onChange={(e) => onChange(e.target.value)} placeholder={f.required ? "Required" : "Empty (imputed)"} />
      </Field>
      {/* The datalist lives outside Field, which clones a single child. Unknown categories are still allowed. */}
      {listId && (
        <datalist id={listId}>
          {f.categories.slice(0, 500).map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
      )}
    </div>
  );
}

function BatchPredict({ model, version }: { model: MlModel; version: ModelVersion }) {
  const { slug } = useOrgContext();
  const datasets = useDatasets(slug);
  const ready = (datasets.data ?? []).filter((d) => String(d.status) === "ready");
  const batch = useBatchPredict(slug, model.id);
  const [datasetId, setDatasetId] = React.useState<string>("");
  const [name, setName] = React.useState("");
  const [job, setJob] = React.useState<{ jobId: string | null; outputId: string | null } | null>(null);
  const [outputId, setOutputId] = React.useState<string | null>(null);

  const start = async () => {
    try {
      const res = await batch.mutateAsync({ dataset_id: datasetId, output_name: name.trim() || undefined, version: version.version });
      setOutputId(null);
      setJob({ jobId: res.job_id, outputId: res.id });
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <Card>
      <CardHeader title="Batch prediction" description="Scores every row of a dataset and publishes the result as a new dataset (with lineage) through the normal ingestion path." icon={<Database />} />
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
          <Field id="batch-ds" label="Input dataset">
            <Select value={datasetId || undefined} onValueChange={setDatasetId}>
              <SelectTrigger aria-label="Input dataset">
                <SelectValue placeholder={datasets.isLoading ? "Loading…" : "Choose a ready dataset"} />
              </SelectTrigger>
              <SelectContent>
                {ready.map((d) => (
                  <SelectItem key={d.id} value={d.id} description={`${formatNumber(d.row_count)} rows`}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field id="batch-name" label="Output dataset name (optional)">
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} placeholder={`${model.name} predictions`} />
          </Field>
          <Button onClick={() => void start()} disabled={!datasetId || (!!job && !outputId && !!job.jobId)} loading={batch.isPending}>
            <Play /> Run batch
          </Button>
        </div>
        {job?.jobId && (
          <JobProgress
            org={slug}
            jobId={job.jobId}
            title="Batch prediction"
            onDone={(s) => {
              const r = (s.result ?? {}) as Record<string, unknown>;
              const out = (typeof r.dataset_id === "string" && r.dataset_id) || (typeof r.output_dataset_id === "string" && r.output_dataset_id) || job.outputId;
              setOutputId(s.status === "succeeded" ? (out ?? null) : null);
              if (s.status === "succeeded") setJob((j) => (j ? { ...j, jobId: null } : j));
            }}
          />
        )}
        {outputId && (
          <p className="flex items-center gap-2 text-sm">
            <Check className="size-4 text-success" /> Predictions written.{" "}
            <Link href={`/o/${slug}/data/datasets/${outputId}`} className="font-medium text-accent hover:underline">
              Open the output dataset
            </Link>
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/* ---------------- API usage ---------------- */

interface ApiKeyRow {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  status: string;
  last_used_at: string | null;
}

function ApiPanel({ model, version }: { model: MlModel; version: ModelVersion }) {
  const { slug, can } = useOrgContext();
  const canKeys = can("api_keys.manage");
  const keys = useQuery({
    queryKey: ["org", slug, "api-keys"],
    enabled: canKeys,
    retry: false,
    queryFn: async ({ signal }) => unwrapList(await api.get<ApiKeyRow[] | Paginated<ApiKeyRow>>(`/orgs/${encodeURIComponent(slug)}/api-keys`, { signal })),
  });
  const mlKeys = (keys.data ?? []).filter((k) => k.scopes.includes("ml.predict") && (k.status ?? "active") === "active");
  const [prefix, setPrefix] = React.useState<string | null>(null);
  const chosen = prefix ?? mlKeys[0]?.prefix ?? null;
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const text = curlExample({ origin, org: slug, modelId: model.id, keyPrefix: chosen, schema: version.feature_schema });
  const [copied, setCopied] = React.useState(false);

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
      <Card>
        <CardHeader
          title="Call the prediction API"
          description="Up to 1,000 rows per request, validated against the feature schema. Unknown columns are rejected with a 400 listing them."
          icon={<Code2 />}
          actions={
            <Button
              size="xs"
              variant="secondary"
              onClick={() => {
                void navigator.clipboard?.writeText(text);
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              }}
            >
              {copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy"}
            </Button>
          }
        />
        <CardContent>
          <pre className="scrollbar-thin overflow-x-auto rounded-control bg-surface-2 p-4 font-mono text-[12px] leading-5 text-fg">{text}</pre>
          <p className="mt-3 text-xs text-fg-muted">
            Replace <span className="font-mono">&lt;secret&gt;</span> with the key&apos;s secret. Secrets are shown once, when a key is created, and are never displayed again.
            The response is <span className="font-mono">{"{predictions: [{value, probability, explanation}], model_version, request_id}"}</span>.
          </p>
        </CardContent>
      </Card>
      <Card className="self-start">
        <CardHeader title="API keys with ml.predict" icon={<KeyRound />} />
        <CardContent className="space-y-3 text-sm">
          {!canKeys ? (
            <p className="text-fg-muted">An admin with api_keys.manage can create an org-bound key with the ml.predict scope.</p>
          ) : keys.isLoading ? (
            <Skeleton className="h-16" />
          ) : keys.isError ? (
            <ErrorState compact error={keys.error} />
          ) : mlKeys.length === 0 ? (
            <p className="text-fg-muted">No active key has the ml.predict scope yet. Create one in the API keys settings; the example uses a placeholder until then.</p>
          ) : (
            <ul className="space-y-1.5">
              {mlKeys.map((k) => (
                <li key={k.id}>
                  <button
                    type="button"
                    onClick={() => setPrefix(k.prefix)}
                    aria-pressed={chosen === k.prefix}
                    className={cn("flex w-full items-center gap-2 rounded-control border px-3 py-2 text-left transition-colors focus-ring", chosen === k.prefix ? "border-accent bg-accent-soft/50" : "border-border hover:bg-surface-2")}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium text-fg">{k.name}</span>
                      <span className="font-mono text-xs text-fg-muted">nxk_{k.prefix}_…</span>
                    </span>
                    <span className="text-xs text-fg-subtle">{k.last_used_at ? `used ${formatRelative(k.last_used_at)}` : "never used"}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/* ---------------- monitoring ---------------- */

function MonitoringTab({ model }: { model: MlModel }) {
  const { slug } = useOrgContext();
  const q = useMlMonitoring(slug, model.id);
  if (q.isError) {
    if (isApiError(q.error) && q.error.status === 404 && !isUnavailableEndpoint(q.error))
      return (
        <Card>
          <EmptyState icon={<Activity />} title="No monitoring data yet" />
        </Card>
      );
    return (
      <Card>
        <MlError error={q.error} what="Monitoring" onRetry={() => void q.refetch()} />
      </Card>
    );
  }
  if (q.isLoading || !q.data) return <Skeleton className="h-64 rounded-card" />;
  const m = q.data;
  const counts = { stable: 0, moderate: 0, significant: 0 };
  for (const f of m.features) counts[f.level]++;
  if (!m.features.length && !m.prediction_distribution.length) {
    return (
      <Card>
        <EmptyState
          icon={<Activity />}
          title="No drift report yet"
          description="Drift is computed hourly from recent prediction inputs against the training snapshot. Make predictions through the API or playground to start monitoring."
        />
      </Card>
    );
  }
  return (
    <div className="space-y-5">
      {m.alert && (
        <div role="alert" className="flex items-start gap-2 rounded-card border border-danger/30 bg-danger-soft/50 p-3 text-sm text-fg">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-danger" />
          Significant drift detected. Retraining is suggested; it never happens automatically.
        </div>
      )}
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Drift summary">
        <StatCard label="Stable features" value={formatNumber(counts.stable)} accent="success" hint={`PSI < ${PSI_STABLE}`} />
        <StatCard label="Moderate shift" value={formatNumber(counts.moderate)} accent="warning" hint={`PSI ${PSI_STABLE}–${PSI_MODERATE}`} />
        <StatCard label="Significant drift" value={formatNumber(counts.significant)} accent="warning" hint={`PSI ≥ ${PSI_MODERATE}`} />
        <StatCard label="Predictions in window" value={m.predictions_count === null ? "—" : formatNumber(m.predictions_count)} hint={m.window.start ? `${formatDateTime(m.window.start)} → ${formatDateTime(m.window.end)}` : undefined} />
      </section>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
        <ChartCard
          title="Feature drift (PSI)"
          description="Population stability index per feature: green < 0.1, amber < 0.25, red otherwise."
          height={Math.max(160, Math.min(30, m.features.length) * 26 + 40)}
          empty={!m.features.length ? <EmptyState compact title="No feature PSI" /> : undefined}
          table={{
            columns: [
              { key: "feature", label: "Feature" },
              { key: "psi", label: "PSI", align: "right", format: (v) => Number(v).toFixed(3) },
              { key: "level", label: "Status", format: (v) => PSI_TONE[v as keyof typeof PSI_TONE].label },
            ],
            rows: m.features as unknown as Array<Record<string, unknown>>,
          }}
        >
          <PsiBars features={m.features} />
        </ChartCard>
        <div className="space-y-5">
          <Card>
            <CardHeader title="Drift by feature" />
            <ul className="max-h-72 space-y-1 overflow-y-auto px-5 pb-4 text-sm scrollbar-thin">
              {m.features.slice(0, 30).map((f) => (
                <li key={f.feature} className="flex items-center gap-2">
                  <span className="truncate font-mono text-[13px] text-fg">{f.feature}</span>
                  <span className="tabular ml-auto text-xs text-fg-muted">{f.psi.toFixed(3)}</span>
                  <Badge tone={PSI_TONE[f.level].tone}>{PSI_TONE[f.level].label}</Badge>
                </li>
              ))}
            </ul>
          </Card>
          {Object.keys(m.performance).length > 0 && (
            <Card>
              <CardHeader title="Live performance" description="From ground-truth labels that arrived after prediction." />
              <dl className="grid grid-cols-2 gap-3 px-5 pb-4 text-sm">
                {Object.entries(m.performance).map(([k, v]) => (
                  <div key={k}>
                    <dt className="font-mono text-xs text-fg-subtle">{k}</dt>
                    <dd className="tabular font-semibold text-fg">{fmtMetric(v)}</dd>
                  </div>
                ))}
              </dl>
            </Card>
          )}
        </div>
      </div>
      {m.prediction_distribution.length > 0 && (
        <ChartCard
          title="Prediction distribution"
          description={m.prediction_psi !== null ? `Training vs recent predictions · PSI ${m.prediction_psi.toFixed(3)}` : "Training vs recent predictions"}
          table={{
            columns: [
              { key: "bin", label: "Bin" },
              { key: "reference", label: "Training", align: "right", format: (v) => fmtMetric(v) },
              { key: "current", label: "Recent", align: "right", format: (v) => fmtMetric(v) },
            ],
            rows: m.prediction_distribution,
          }}
        >
          <DistributionCompare data={m.prediction_distribution} />
        </ChartCard>
      )}
    </div>
  );
}
