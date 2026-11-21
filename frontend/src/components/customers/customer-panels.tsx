"use client";

import * as React from "react";
import Link from "next/link";
import { BrainCircuit, Database, Layers, Plus, RefreshCw, Save, Trash2, Upload, X } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { DatasetPicker } from "@/components/ask/dataset-picker";
import { JobProgress } from "@/components/jobs/job-progress";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Pager } from "@/components/ui/pager";
import { StatusBadge } from "@/components/ui/status";
import { isApiError } from "@/lib/api";
import { errorMessage } from "@/lib/errors";
import { buildDefinition, describeDefinition, SEGMENT_FIELDS, SEGMENT_OPS, type DraftCondition } from "@/lib/mdm";
import {
  EVENT_FIELDS,
  PROFILE_FIELDS,
  useCreateCustomerSource,
  useCreateSegment,
  useCustomerSegments,
  useCustomerSettings,
  useCustomerSources,
  useDeleteCustomerSource,
  useDeleteSegment,
  usePublishFeatures,
  useRefreshSegment,
  useSegmentMembers,
  useStartScoring,
  useUpdateCustomerSettings,
  type CustomerSegment,
  type CustomerSource,
  type CustomerSourceKind,
} from "@/lib/queries/customers";
import { useDatasets } from "@/lib/queries/datasets";
import { useMlModels } from "@/lib/queries/ml";
import { formatNumber, formatRelative, titleCase } from "@/lib/utils";
import { ColumnMapping, excludedByPolicy, ModuleError, NativeSelect, type MappingField } from "@/components/mdm/common";

/* ---------------- sources ---------------- */

const FIELD_LABELS: Record<string, string> = {
  external_id: "Customer id",
  email: "Email",
  phone: "Phone",
  name: "Full name",
  first_name: "First name",
  last_name: "Last name",
  address: "Address",
  dob: "Date of birth",
  created_at: "Created at",
  customer_key: "Customer key (profile source id)",
  occurred_at: "Occurred at",
  event_type_column: "Event type column",
  amount: "Amount",
  channel: "Channel",
  event_id: "Event id",
};

export function SourcesPanel() {
  const { slug, can } = useOrgContext();
  const canManage = can("customers.manage");
  const sources = useCustomerSources(slug);
  const datasets = useDatasets(slug);
  const del = useDeleteCustomerSource(slug);
  const [open, setOpen] = React.useState(false);
  const [pending, setPending] = React.useState<CustomerSource | null>(null);
  const dsName = (id: string) => (datasets.data ?? []).find((d) => d.id === id)?.name ?? "Dataset";

  if (sources.isError) return <Card><ModuleError error={sources.error} what="Customer sources" onRetry={() => void sources.refetch()} /></Card>;
  return (
    <Card>
      <CardHeader
        title="Sources"
        description="Datasets mapped to customer fields. Profile sources feed identity resolution; event sources (orders, tickets, visits) feed timelines and RFM."
        actions={
          canManage && (
            <Button size="sm" onClick={() => setOpen(true)}>
              <Plus /> Map a dataset
            </Button>
          )
        }
      />
      <CardContent>
        {sources.isLoading ? (
          <Skeleton className="h-32" />
        ) : !(sources.data ?? []).length ? (
          <EmptyState compact icon={<Database />} title="No sources mapped" description="Map a CRM or web signup dataset to start." />
        ) : (
          <ul className="divide-y divide-border">
            {sources.data!.map((s) => (
              <li key={s.id} className="flex flex-wrap items-start justify-between gap-3 py-3">
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{s.name}</span>
                    <Badge tone={s.kind === "profile" ? "accent" : "teal"}>{s.kind === "profile" ? "Profiles" : "Events"}</Badge>
                    <StatusBadge status={s.status} />
                    <span className="text-xs text-fg-subtle">priority {s.priority}</span>
                  </div>
                  <p className="text-xs text-fg-muted">
                    <Link href={`/o/${slug}/data/datasets/${s.dataset_id}`} className="hover:underline">
                      {dsName(s.dataset_id)}
                    </Link>{" "}
                    · {formatNumber(s.record_count)} records · {s.last_synced_at ? `synced ${formatRelative(s.last_synced_at)}` : "not synced yet"}
                  </p>
                  <p className="text-xs text-fg-subtle">
                    {Object.entries(s.mapping)
                      .map(([k, v]) => `${FIELD_LABELS[k] ?? k} ← ${Array.isArray(v) ? v.join(", ") : v}`)
                      .join(" · ")}
                  </p>
                  {s.last_error && <p className="text-xs text-danger">{s.last_error}</p>}
                </div>
                {canManage && (
                  <Button variant="ghost" size="icon-sm" aria-label={`Remove ${s.name}`} onClick={() => setPending(s)}>
                    <Trash2 />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
      <AddSourceDialog open={open} onOpenChange={setOpen} sources={sources.data ?? []} />
      <ConfirmDialog
        open={!!pending}
        onOpenChange={(o) => !o && setPending(null)}
        title={`Remove "${pending?.name ?? ""}"?`}
        description="Its records leave identity resolution on the next run. The dataset itself is not touched."
        confirmLabel="Remove source"
        loading={del.isPending}
        onConfirm={async () => {
          if (!pending) return;
          try {
            await del.mutateAsync(pending.id);
            toast.success("Source removed");
            setPending(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </Card>
  );
}

function AddSourceDialog({ open, onOpenChange, sources }: { open: boolean; onOpenChange: (o: boolean) => void; sources: CustomerSource[] }) {
  const { slug } = useOrgContext();
  const datasets = useDatasets(slug);
  const create = useCreateCustomerSource(slug);
  const [kind, setKind] = React.useState<CustomerSourceKind>("profile");
  const [datasetId, setDatasetId] = React.useState<string | null>(null);
  const [mapping, setMapping] = React.useState<Record<string, string | string[]>>({});
  const [name, setName] = React.useState("");
  const [priority, setPriority] = React.useState("100");
  const [eventType, setEventType] = React.useState("order");
  const [link, setLink] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const profiles = sources.filter((s) => s.kind === "profile");

  React.useEffect(() => {
    if (!open) {
      setDatasetId(null);
      setMapping({});
      setName("");
      setError(null);
    }
  }, [open]);

  const fields: MappingField[] =
    kind === "profile"
      ? PROFILE_FIELDS.map((k) => ({ key: k, label: FIELD_LABELS[k] ?? k }))
      : [
          ...EVENT_FIELDS.map((k) => ({ key: k, label: FIELD_LABELS[k] ?? k, hint: k === "event_type_column" ? "Optional: a column holding the event type per row." : undefined })),
          { key: "attributes", label: "Extra attributes (up to 20)", multi: true },
        ];

  const submit = async () => {
    setError(null);
    if (!datasetId) return setError("Choose a dataset.");
    const body = {
      dataset_id: datasetId,
      kind,
      mapping: kind === "event" ? { ...mapping, event_type: eventType } : mapping,
      name: name.trim() || undefined,
      priority: Number(priority) || 100,
      link_source_id: kind === "event" ? link || undefined : undefined,
    };
    try {
      await create.mutateAsync(body);
      toast.success("Source mapped. Run identity resolution to load it.");
      onOpenChange(false);
    } catch (e) {
      const blocked = excludedByPolicy(e);
      setError(blocked.length ? `Masked or denied for you by the data policy: ${blocked.join(", ")}` : errorMessage(e));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader title="Map a dataset to customer fields" description="Only columns you may query unmasked can be mapped. Lower priority numbers win survivorship ties." />
        <DialogBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="src-kind" label="Kind">
              <NativeSelect id="src-kind" value={kind} onChange={(e) => (setKind(e.target.value as CustomerSourceKind), setMapping({}))}>
                <option value="profile">Customer profiles</option>
                <option value="event" disabled={!profiles.length}>
                  Events (orders, tickets, visits){!profiles.length ? " — map a profile source first" : ""}
                </option>
              </NativeSelect>
            </Field>
            <div>
              <span className="mb-1 block text-xs font-medium">Dataset</span>
              <DatasetPicker datasets={datasets.data ?? []} value={datasetId} onChange={(id) => (setDatasetId(id), setMapping({}))} />
            </div>
            <Field id="src-name" label="Name" hint="Defaults to the dataset name.">
              <Input id="src-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </Field>
            <Field id="src-priority" label="Priority">
              <Input id="src-priority" type="number" min={0} max={10000} value={priority} onChange={(e) => setPriority(e.target.value)} />
            </Field>
            {kind === "event" && (
              <>
                <Field id="src-link" label="Linked profile source" hint="Its customer id is what the customer key column holds.">
                  <NativeSelect id="src-link" value={link} onChange={(e) => setLink(e.target.value)}>
                    <option value="">Choose…</option>
                    {profiles.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
                <Field id="src-etype" label="Event type" hint="Used when no event type column is mapped. Orders drive RFM.">
                  <Input id="src-etype" value={eventType} onChange={(e) => setEventType(e.target.value)} maxLength={40} />
                </Field>
              </>
            )}
          </div>
          <ColumnMapping org={slug} datasetId={datasetId} fields={fields} value={mapping} onChange={setMapping} />
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending}>
            Save mapping
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------- segments ---------------- */

export function SegmentsPanel() {
  const { slug } = useOrgContext();
  const segments = useCustomerSegments(slug);
  const create = useCreateSegment(slug);
  const refresh = useRefreshSegment(slug);
  const del = useDeleteSegment(slug);
  const [builder, setBuilder] = React.useState(false);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState<CustomerSegment | null>(null);

  const rfm = async () => {
    try {
      await create.mutateAsync({ method: "rfm" });
      toast.success("RFM segments refreshed");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  if (segments.isError) return <Card><ModuleError error={segments.error} what="Segments" onRetry={() => void segments.refetch()} /></Card>;
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <Card>
        <CardHeader
          title="Segments"
          description="Rule segments are re-evaluated after every resolution run; RFM segments use recency/frequency/monetary quintiles."
          actions={
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" onClick={rfm} loading={create.isPending}>
                <RefreshCw /> RFM segments
              </Button>
              <Button size="sm" onClick={() => setBuilder(true)}>
                <Plus /> New segment
              </Button>
            </div>
          }
        />
        <CardContent>
          {segments.isLoading ? (
            <Skeleton className="h-40" />
          ) : !(segments.data ?? []).length ? (
            <EmptyState compact icon={<Layers />} title="No segments yet" description="Generate RFM segments or build a rule segment." />
          ) : (
            <ul className="divide-y divide-border">
              {segments.data!.map((s) => (
                <li key={s.id} className={`flex items-start justify-between gap-2 py-2.5 ${selected === s.id ? "bg-surface-2/60" : ""}`}>
                  <button type="button" className="min-w-0 flex-1 text-left focus-ring rounded-[4px]" onClick={() => setSelected(s.id)}>
                    <span className="flex items-center gap-2">
                      <span className="font-medium">{s.name}</span>
                      <Badge tone={s.method === "rfm" ? "teal" : "accent"}>{s.method.toUpperCase()}</Badge>
                      <span className="tabular text-xs text-fg-muted">{formatNumber(s.size)} customers</span>
                    </span>
                    <span className="mt-0.5 block truncate text-xs text-fg-subtle">{describeDefinition(s.definition) || s.description}</span>
                  </button>
                  <span className="flex shrink-0 gap-1">
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Refresh ${s.name}`}
                      onClick={async () => {
                        try {
                          await refresh.mutateAsync(s.id);
                          toast.success("Segment refreshed");
                        } catch (e) {
                          toast.error(errorMessage(e));
                        }
                      }}
                    >
                      <RefreshCw />
                    </Button>
                    <Button variant="ghost" size="icon-xs" aria-label={`Delete ${s.name}`} onClick={() => setPending(s)}>
                      <Trash2 />
                    </Button>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
      <SegmentMembers id={selected} name={(segments.data ?? []).find((s) => s.id === selected)?.name} />
      <SegmentBuilder open={builder} onOpenChange={setBuilder} />
      <ConfirmDialog
        open={!!pending}
        onOpenChange={(o) => !o && setPending(null)}
        title={`Delete "${pending?.name ?? ""}"?`}
        confirmLabel="Delete segment"
        loading={del.isPending}
        onConfirm={async () => {
          if (!pending) return;
          try {
            await del.mutateAsync(pending.id);
            if (selected === pending.id) setSelected(null);
            setPending(null);
            toast.success("Segment deleted");
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}

function SegmentMembers({ id, name }: { id: string | null; name?: string }) {
  const { slug } = useOrgContext();
  const members = useSegmentMembers(slug, id);
  return (
    <Card>
      <CardHeader title={name ? `Members of ${name}` : "Members"} description={id ? undefined : "Select a segment to see its customers."} />
      <CardContent>
        {!id ? null : members.isError ? (
          <ModuleError compact error={members.error} what="Segment members" />
        ) : members.isLoading ? (
          <Skeleton className="h-32" />
        ) : !members.items?.length ? (
          <EmptyState compact icon={<Layers />} title="No members" description="No customer matches this segment right now." />
        ) : (
          <>
            <ul className="divide-y divide-border text-sm">
              {members.items.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2 py-2">
                  <Link href={`/o/${slug}/customers/${p.id}`} className="truncate font-medium hover:underline">
                    {p.display_name || "Unnamed customer"}
                  </Link>
                  <span className="tabular text-xs text-fg-muted">
                    {formatNumber(p.order_count)} orders · {formatNumber(p.monetary)}
                    {p.segment && typeof p.segment.rfm === "string" ? ` · RFM ${p.segment.rfm}` : ""}
                  </span>
                </li>
              ))}
            </ul>
            <Pager page={members.page} hasPrev={members.hasPrev} hasNext={members.hasNext} onPrev={members.goPrev} onNext={members.goNext} loading={members.isFetching} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

function SegmentBuilder({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { slug } = useOrgContext();
  const create = useCreateSegment(slug);
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [match, setMatch] = React.useState<"all" | "any">("all");
  const [rows, setRows] = React.useState<DraftCondition[]>([{ field: "order_count", op: "gte", value: "" }]);
  const [errors, setErrors] = React.useState<Record<number, string>>({});
  const [serverError, setServerError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) {
      setName("");
      setDescription("");
      setRows([{ field: "order_count", op: "gte", value: "" }]);
      setErrors({});
      setServerError(null);
    }
  }, [open]);

  const save = async () => {
    setServerError(null);
    if (!name.trim()) return setServerError("Name the segment.");
    const { definition, errors: errs } = buildDefinition(match, rows);
    setErrors(errs);
    if (!definition) return;
    try {
      await create.mutateAsync({ method: "rule", name: name.trim(), description: description.trim() || undefined, definition });
      toast.success("Segment created");
      onOpenChange(false);
    } catch (e) {
      setServerError(errorMessage(e));
    }
  };

  const update = (i: number, patch: Partial<DraftCondition>) => setRows((r) => r.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader title="New rule segment" description="Conditions are evaluated on each golden profile and its RFM scores." />
        <DialogBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="seg-name" label="Name">
              <Input id="seg-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </Field>
            <Field id="seg-desc" label="Description">
              <Input id="seg-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} />
            </Field>
          </div>
          <div className="flex items-center gap-2 text-sm">
            Customers matching
            <NativeSelect value={match} onChange={(e) => setMatch(e.target.value as "all" | "any")} className="w-auto" aria-label="Match mode">
              <option value="all">all</option>
              <option value="any">any</option>
            </NativeSelect>
            of these conditions:
          </div>
          <ul className="space-y-2">
            {rows.map((row, i) => {
              const op = SEGMENT_OPS.find((o) => o.value === row.op);
              return (
                <li key={i} className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <NativeSelect value={row.field} onChange={(e) => update(i, { field: e.target.value })} className="w-48" aria-label={`Condition ${i + 1} field`}>
                      {SEGMENT_FIELDS.map((f) => (
                        <option key={f.value} value={f.value}>
                          {f.label}
                        </option>
                      ))}
                    </NativeSelect>
                    <NativeSelect value={row.op} onChange={(e) => update(i, { op: e.target.value })} className="w-32" aria-label={`Condition ${i + 1} operator`}>
                      {SEGMENT_OPS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </NativeSelect>
                    {op?.needsValue !== false && (
                      <Input
                        value={row.value}
                        onChange={(e) => update(i, { value: e.target.value })}
                        placeholder={op?.list ? "a, b, c" : "value"}
                        className="h-9 w-40"
                        aria-label={`Condition ${i + 1} value`}
                        aria-invalid={!!errors[i]}
                      />
                    )}
                    <Button variant="ghost" size="icon-sm" aria-label={`Remove condition ${i + 1}`} onClick={() => setRows((r) => r.filter((_, j) => j !== i))} disabled={rows.length === 1}>
                      <X />
                    </Button>
                  </div>
                  {errors[i] && <p className="text-xs text-danger">{errors[i]}</p>}
                </li>
              );
            })}
          </ul>
          <Button variant="secondary" size="sm" onClick={() => setRows((r) => [...r, { field: "monetary", op: "gte", value: "" }])} disabled={rows.length >= 20}>
            <Plus /> Add condition
          </Button>
          {(serverError || errors[-1]) && (
            <p role="alert" className="text-sm text-danger">
              {serverError ?? errors[-1]}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={save} loading={create.isPending}>
            Create segment
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------- scoring ---------------- */

export function ScoringPanel() {
  const { slug, can } = useOrgContext();
  const canManage = can("customers.manage");
  const models = useMlModels(slug);
  const start = useStartScoring(slug);
  const publish = usePublishFeatures(slug);
  const [scoreType, setScoreType] = React.useState<"churn" | "clv">("churn");
  const [modelId, setModelId] = React.useState("");
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [needsModel, setNeedsModel] = React.useState(false);
  const wanted = scoreType === "churn" ? "classification" : "regression";
  const candidates = (models.data ?? []).filter((m) => !m.task_type || m.task_type === wanted);

  const run = async () => {
    setNeedsModel(false);
    try {
      const res = await start.mutateAsync({ score_type: scoreType, model_id: modelId });
      setJobId(res.job_id);
      toast.success("Scoring started");
    } catch (e) {
      if (isApiError(e) && e.code === "ml_model_required") setNeedsModel(true);
      else toast.error(errorMessage(e));
    }
  };

  return (
    <Card>
      <CardHeader title="Scoring" description="Churn (classification) and lifetime value (regression) scores come from your own models in the ML workspace, with per-customer explanations." />
      <CardContent className="space-y-4">
        {!canManage ? (
          <p className="text-sm text-fg-muted">Scoring needs the customers.manage permission.</p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field id="score-type" label="Score">
                <NativeSelect id="score-type" value={scoreType} onChange={(e) => (setScoreType(e.target.value as "churn" | "clv"), setModelId(""))}>
                  <option value="churn">Churn risk</option>
                  <option value="clv">Customer lifetime value</option>
                </NativeSelect>
              </Field>
              <Field id="score-model" label="Model" hint={models.isError ? errorMessage(models.error) : `${titleCase(wanted)} models with a production or staging version.`}>
                <NativeSelect id="score-model" value={modelId} onChange={(e) => setModelId(e.target.value)}>
                  <option value="">Choose a model…</option>
                  {candidates.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                      {m.production_version ? ` (v${m.production_version} production)` : m.latest_version ? ` (latest v${m.latest_version})` : ""}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button onClick={run} loading={start.isPending} disabled={!modelId || !!jobId}>
                <BrainCircuit /> Score customers
              </Button>
              <Button
                variant="secondary"
                loading={publish.isPending}
                onClick={async () => {
                  try {
                    const res = await publish.mutateAsync();
                    toast.success(`Published features as version ${res.version}`);
                  } catch (e) {
                    toast.error(errorMessage(e));
                  }
                }}
              >
                <Upload /> Publish features dataset
              </Button>
            </div>
            <p className="text-xs text-fg-subtle">
              The features dataset (record/order counts, RFM, tenure; no personal data) is what you train churn and CLV models on in the{" "}
              <Link href={`/o/${slug}/ml`} className="text-accent hover:underline">
                ML workspace
              </Link>
              .
            </p>
            {needsModel && (
              <p role="alert" className="rounded-control border border-warning/40 bg-warning-soft p-3 text-sm">
                This model has no production or staging version yet. Promote a version in the ML workspace, then score again.
              </p>
            )}
            {jobId && <JobProgress org={slug} jobId={jobId} title="Scoring customers" onDone={() => setTimeout(() => setJobId(null), 4000)} />}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/* ---------------- settings ---------------- */

export function CustomerSettingsPanel() {
  const { slug, can } = useOrgContext();
  const canManage = can("customers.manage");
  const settings = useCustomerSettings(slug);
  const save = useUpdateCustomerSettings(slug);
  const [auto, setAuto] = React.useState("");
  const [review, setReview] = React.useState("");
  const [weights, setWeights] = React.useState<Record<string, string>>({});
  const [surv, setSurv] = React.useState<Record<string, string>>({});
  const [country, setCountry] = React.useState("");
  const data = settings.data;

  React.useEffect(() => {
    if (!data) return;
    setAuto(String(data.auto_threshold));
    setReview(String(data.review_threshold));
    setWeights(Object.fromEntries(Object.entries(data.weights).map(([k, v]) => [k, String(v)])));
    setSurv({ ...data.survivorship });
    setCountry(data.default_country_code);
  }, [data]);

  if (settings.isError) return <Card><ModuleError error={settings.error} what="Matching settings" onRetry={() => void settings.refetch()} /></Card>;
  if (!data) return <Skeleton className="h-64 rounded-card" />;
  const submit = async () => {
    try {
      await save.mutateAsync({
        auto_threshold: Number(auto),
        review_threshold: Number(review),
        weights: Object.fromEntries(Object.entries(weights).map(([k, v]) => [k, Number(v)])),
        survivorship: surv,
        default_country_code: country,
      });
      toast.success("Settings saved. They apply from the next resolution run.");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  return (
    <Card>
      <CardHeader
        title="Matching settings"
        description="Pairs scoring at or above the auto threshold merge automatically; pairs between the review and auto thresholds go to the review queue."
        actions={
          canManage && (
            <Button size="sm" onClick={submit} loading={save.isPending}>
              <Save /> Save
            </Button>
          )
        }
      />
      <CardContent className="grid gap-6 lg:grid-cols-3">
        <fieldset disabled={!canManage} className="space-y-3">
          <legend className="mb-1 text-sm font-medium">Thresholds</legend>
          <Field id="cs-auto" label="Auto-merge at" hint={`Default ${data.defaults.auto_threshold}`}>
            <Input id="cs-auto" type="number" step="0.01" min={0} max={1} value={auto} onChange={(e) => setAuto(e.target.value)} />
          </Field>
          <Field id="cs-review" label="Review from" hint={`Default ${data.defaults.review_threshold}`}>
            <Input id="cs-review" type="number" step="0.01" min={0} max={1} value={review} onChange={(e) => setReview(e.target.value)} />
          </Field>
          <Field id="cs-cc" label="Default country calling code" hint="Used to normalize phone numbers without one.">
            <Input id="cs-cc" value={country} onChange={(e) => setCountry(e.target.value)} maxLength={4} />
          </Field>
        </fieldset>
        <fieldset disabled={!canManage} className="space-y-3">
          <legend className="mb-1 text-sm font-medium">Feature weights</legend>
          {Object.keys(data.defaults.weights).map((k) => (
            <Field key={k} id={`cs-w-${k}`} label={titleCase(k)} hint={`Default ${data.defaults.weights[k]}`}>
              <Input id={`cs-w-${k}`} type="number" step="0.01" min={0} max={10} value={weights[k] ?? ""} onChange={(e) => setWeights((w) => ({ ...w, [k]: e.target.value }))} />
            </Field>
          ))}
        </fieldset>
        <fieldset disabled={!canManage} className="space-y-3">
          <legend className="mb-1 text-sm font-medium">Survivorship (golden record)</legend>
          {Object.keys(data.defaults.survivorship).map((k) => (
            <Field key={k} id={`cs-s-${k}`} label={titleCase(k.replace(/_/g, " "))}>
              <NativeSelect id={`cs-s-${k}`} value={surv[k] ?? ""} onChange={(e) => setSurv((s) => ({ ...s, [k]: e.target.value }))}>
                {data.defaults.survivorship_rules.map((r) => (
                  <option key={r} value={r}>
                    {titleCase(r.replace(/_/g, " "))}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          ))}
        </fieldset>
      </CardContent>
    </Card>
  );
}
