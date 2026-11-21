"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, CheckCircle2, History, Pause, Play, Rocket, Save, Trash2, TriangleAlert, Workflow as WorkflowIcon } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CATEGORY_LABELS, stepKind, type StepCategory, type StepKind } from "@/lib/automation/catalog";
import { graphToWire, validateGraph, type WfGraph, type Workflow, type WorkflowVersion } from "@/lib/automation/graph";
import { errorMessage } from "@/lib/errors";
import { useDeleteWorkflow, usePublishWorkflow, useSaveVersion, useStartRun, useStepCatalog, useUpdateWorkflow, useWorkflow, useWorkflowVersions } from "@/lib/queries/automation";
import { formatRelative } from "@/lib/utils";
import { ModuleError } from "@/components/mdm/common";
import { RunsTable } from "./automation-view";
import { ConfigForm, JsonInput } from "./config-form";
import { TriggersPanel } from "./triggers";
import type { CanvasApi } from "./workflow-canvas";
import { DND_TYPE, stepIcon } from "./workflow-canvas-shared";

// The flow library is heavy: load the canvas only on this route, client-side.
const WorkflowCanvas = dynamic(() => import("./workflow-canvas"), { ssr: false, loading: () => <Skeleton className="h-full w-full" /> });

export function WorkflowEditor() {
  const { id } = useParams<{ id: string }>();
  const { slug } = useOrgContext();
  const wf = useWorkflow(slug, id);
  const versions = useWorkflowVersions(slug, id);
  const catalog = useStepCatalog(slug);
  useSetBreadcrumb(wf.data?.name);
  if (wf.isError) {
    return (
      <Card>
        <ModuleError error={wf.error} what="This workflow" onRetry={() => void wf.refetch()} />
        <div className="flex justify-center pb-6">
          <Button variant="secondary" asChild>
            <Link href={`/o/${slug}/automation`}>
              <ArrowLeft /> Automation
            </Link>
          </Button>
        </div>
      </Card>
    );
  }
  if (!wf.data || versions.isLoading || catalog.isLoading) return <Skeleton className="h-[640px] rounded-card" />;
  const latest = versions.data?.[0] ?? null;
  return <EditorInner key={`${wf.data.id}:${latest?.id ?? "none"}`} workflow={wf.data} latest={latest} versions={versions.data ?? []} catalog={catalog.data?.steps ?? []} registryKnown={!!catalog.data?.fromServer} />;
}

function EditorInner({ workflow, latest, versions, catalog, registryKnown }: { workflow: Workflow; latest: WorkflowVersion | null; versions: WorkflowVersion[]; catalog: StepKind[]; registryKnown: boolean }) {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const canManage = can("automation.manage");
  const canRun = can("automation.run");
  const initial = React.useMemo<WfGraph>(() => latest?.graph ?? workflow.graph ?? { nodes: [], edges: [] }, [latest, workflow.graph]);
  const [graph, setGraph] = React.useState<WfGraph>(initial);
  const [savedJson, setSavedJson] = React.useState(() => JSON.stringify(graphToWire(initial)));
  const [selected, setSelected] = React.useState<string | null>(null);
  const [name, setName] = React.useState(workflow.name);
  const [runOpen, setRunOpen] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const api = React.useRef<CanvasApi | null>(null);
  const save = useSaveVersion(slug, workflow.id);
  const publish = usePublishWorkflow(slug, workflow.id);
  const update = useUpdateWorkflow(slug, workflow.id);
  const del = useDeleteWorkflow(slug);

  const onChange = React.useCallback((g: WfGraph) => setGraph(g), []);
  const issues = React.useMemo(() => validateGraph(graph, catalog), [graph, catalog]);
  const byNode = React.useMemo(() => {
    const m: Record<string, string[]> = {};
    for (const i of issues) if (i.nodeId) (m[i.nodeId] ??= []).push(i.message);
    return m;
  }, [issues]);
  const dirty = JSON.stringify(graphToWire(graph)) !== savedJson;
  const nameDirty = name.trim() !== workflow.name && name.trim().length > 0;
  const published = versions.find((v) => v.published);
  const selNode = graph.nodes.find((n) => n.id === selected) ?? null;
  const selKind = selNode ? stepKind(catalog, selNode.kind) : null;

  const doSave = async (): Promise<WorkflowVersion | null> => {
    if (issues.length) {
      toast.error("Fix the highlighted issues first.");
      return null;
    }
    try {
      if (nameDirty) await update.mutateAsync({ name: name.trim() });
      const v = await save.mutateAsync(graph);
      setSavedJson(JSON.stringify(graphToWire(graph)));
      toast.success(`Saved version ${v.version || ""}`.trim());
      return v;
    } catch (e) {
      toast.error(errorMessage(e));
      return null;
    }
  };

  const doPublish = async () => {
    let version = latest?.version;
    if (dirty || !latest) {
      const v = await doSave();
      if (!v) return;
      version = v.version || undefined;
    }
    try {
      await publish.mutateAsync(version);
      toast.success("Published. Triggers now start this version.");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const setStatus = async (status: "active" | "paused") => {
    try {
      await update.mutateAsync({ status });
      toast.success(status === "active" ? "Workflow activated" : "Workflow paused; triggers won't start runs");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const groups = (Object.keys(CATEGORY_LABELS) as StepCategory[]).map((cat) => ({ cat, steps: catalog.filter((s) => s.category === cat) })).filter((g) => g.steps.length);

  return (
    <div className="space-y-4">
      <Card className="flex flex-wrap items-center gap-2 px-3 py-2">
        <Button variant="ghost" size="icon-sm" asChild>
          <Link href={`/o/${slug}/automation`} aria-label="Back to automation">
            <ArrowLeft />
          </Link>
        </Button>
        <Input aria-label="Workflow name" value={name} onChange={(e) => setName(e.target.value)} disabled={!canManage} className="h-8 w-56 border-transparent bg-transparent font-semibold shadow-none hover:border-border" />
        <StatusBadge status={workflow.status} />
        {published ? <Badge tone="success">Published v{published.version}</Badge> : <Badge tone="outline">Not published</Badge>}
        {(dirty || nameDirty) && canManage && <Badge tone="warning">Unsaved</Badge>}
        <span className={`inline-flex items-center gap-1 text-xs ${issues.length ? "text-danger" : "text-success"}`} title={issues.map((i) => i.message).join("\n")}>
          {issues.length ? <TriangleAlert className="size-3.5" /> : <CheckCircle2 className="size-3.5" />}
          {issues.length ? `${issues.length} issue${issues.length === 1 ? "" : "s"}` : "Valid"}
        </span>
        <div className="ml-auto flex flex-wrap gap-1.5">
          {canManage && workflow.status === "active" && (
            <Button size="sm" variant="secondary" onClick={() => void setStatus("paused")} disabled={update.isPending}>
              <Pause /> Pause
            </Button>
          )}
          {canManage && workflow.status !== "active" && published && (
            <Button size="sm" variant="secondary" onClick={() => void setStatus("active")} disabled={update.isPending}>
              <Play /> Activate
            </Button>
          )}
          {canRun && (
            <Button size="sm" variant="secondary" onClick={() => setRunOpen(true)} disabled={!published}>
              <Play /> Run
            </Button>
          )}
          {canManage && (
            <>
              <Button size="sm" variant="secondary" onClick={() => void doSave()} loading={save.isPending} disabled={!dirty && !nameDirty}>
                <Save /> Save
              </Button>
              <Button size="sm" onClick={() => void doPublish()} loading={publish.isPending}>
                <Rocket /> Publish
              </Button>
              <Button size="icon-sm" variant="ghost" aria-label="Delete workflow" onClick={() => setDeleting(true)}>
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      </Card>

      {issues.filter((i) => !i.nodeId).length > 0 && (
        <ul className="rounded-control border border-danger/30 bg-danger-soft px-3 py-2 text-sm text-danger">
          {issues
            .filter((i) => !i.nodeId)
            .map((i) => (
              <li key={i.message}>{i.message}</li>
            ))}
        </ul>
      )}

      <div className="grid gap-4 lg:grid-cols-[13rem_minmax(0,1fr)_18rem]">
        <Card className="max-h-[600px] overflow-y-auto p-3">
          <p className="mb-2 text-xs font-medium text-fg-muted">Steps {registryKnown ? "" : "(from the contract)"}</p>
          {groups.map((g) => (
            <div key={g.cat} className="mb-3">
              <p className="mb-1 text-[10px] font-semibold text-fg-subtle uppercase">{CATEGORY_LABELS[g.cat]}</p>
              <ul className="space-y-1">
                {g.steps.map((s) => (
                  <li key={s.kind}>
                    <button
                      type="button"
                      draggable={canManage}
                      disabled={!canManage}
                      onDragStart={(e) => e.dataTransfer.setData(DND_TYPE, s.kind)}
                      onClick={() => api.current?.addStep(s)}
                      title={s.description}
                      className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-sm hover:bg-surface-2 focus-ring disabled:opacity-60 [&_svg]:size-3.5 [&_svg]:text-fg-muted"
                    >
                      {stepIcon(s)}
                      <span className="truncate">{s.label}</span>
                      {registryKnown && !s.registered && <span className="ml-auto text-[9px] text-fg-subtle">n/a</span>}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </Card>
        <Card className="h-[600px] overflow-hidden">
          <WorkflowCanvas initial={initial} catalog={catalog} issues={byNode} readOnly={!canManage} onChange={onChange} selected={selected} onSelect={setSelected} apiRef={api} />
        </Card>
        <Card className="max-h-[600px] overflow-y-auto">
          {selNode && selKind ? (
            <>
              <CardHeader title={selKind.label} description={selKind.description} />
              <CardContent className="space-y-3">
                {selKind.impact === "high" && <p className="rounded-control bg-warning-soft px-2 py-1.5 text-xs text-warning">High impact: a run pauses for approval before this step.</p>}
                <Field id="node-name" label="Step name">
                  <Input id="node-name" value={selNode.name} placeholder={selKind.label} onChange={(e) => api.current?.updateNode(selNode.id, { name: e.target.value })} disabled={!canManage} />
                </Field>
                <p className="font-mono text-[11px] text-fg-subtle">id: {selNode.id} · outputs at steps.{selNode.id}</p>
                <ConfigForm key={selNode.id} schema={selKind.schema} value={selNode.config} onChange={(config) => api.current?.updateNode(selNode.id, { config })} disabled={!canManage} />
                <p className="text-[11px] text-fg-subtle">Values may reference the run, e.g. {"{{ trigger.dataset_id }}"} or {"{{ steps.<id>.output.row_count }}"}.</p>
                <div className="grid grid-cols-3 gap-2">
                  {(
                    [
                      ["retries", "Retries", 0, 5],
                      ["retry_backoff_s", "Backoff (s)", 0, 3600],
                      ["timeout_s", "Timeout (s)", 1, 3600],
                    ] as const
                  ).map(([key, label, min, max]) => (
                    <Field key={key} id={`opt-${key}`} label={label}>
                      <Input
                        id={`opt-${key}`}
                        type="number"
                        min={min}
                        max={max}
                        value={selNode.options?.[key] ?? ""}
                        disabled={!canManage}
                        onChange={(e) => {
                          const next = { ...(selNode.options ?? {}) };
                          const v = e.target.value === "" ? undefined : Math.max(min, Math.min(max, Math.trunc(Number(e.target.value))));
                          if (v === undefined || Number.isNaN(v)) delete next[key];
                          else next[key] = v;
                          api.current?.updateNode(selNode.id, { options: next });
                        }}
                      />
                    </Field>
                  ))}
                </div>
                {(byNode[selNode.id] ?? []).map((m) => (
                  <p key={m} className="text-xs text-danger">
                    {m}
                  </p>
                ))}
                {canManage && (
                  <Button variant="secondary" size="sm" onClick={() => api.current?.deleteNode(selNode.id)}>
                    <Trash2 /> Remove step
                  </Button>
                )}
              </CardContent>
            </>
          ) : (
            <CardContent className="space-y-2 pt-4 text-sm text-fg-muted">
              <p>Click a step in the list (or drag it onto the canvas) to add it after the selected step.</p>
              <p>Connect steps left to right. A condition has a yes and a no output.</p>
              <p>Save creates a new version; Publish makes triggers and Run use it.</p>
            </CardContent>
          )}
        </Card>
      </div>

      <Tabs defaultValue="triggers">
        <TabsList aria-label="Workflow details">
          <TabsTrigger value="triggers">Triggers</TabsTrigger>
          <TabsTrigger value="runs">
            <History /> Runs
          </TabsTrigger>
          <TabsTrigger value="versions">
            <WorkflowIcon /> Versions
          </TabsTrigger>
        </TabsList>
        <TabsContent value="triggers">
          <TriggersPanel workflowId={workflow.id} canManage={canManage} />
        </TabsContent>
        <TabsContent value="runs">
          <RunsTable workflowId={workflow.id} />
        </TabsContent>
        <TabsContent value="versions">
          <Card>
            <CardContent className="pt-4">
              {!versions.length ? (
                <p className="text-sm text-fg-muted">No saved versions yet.</p>
              ) : (
                <ul className="divide-y divide-border text-sm">
                  {versions.map((v) => (
                    <li key={v.id || v.version} className="flex items-center justify-between gap-2 py-2">
                      <span className="font-medium">v{v.version}</span>
                      <span className="text-xs text-fg-muted">{v.graph.nodes.length} steps</span>
                      <span className="text-xs text-fg-muted">{formatRelative(v.created_at)}</span>
                      {v.published ? <Badge tone="success">Published {formatRelative(v.published_at)}</Badge> : <Badge tone="outline">Draft</Badge>}
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <RunDialog open={runOpen} onOpenChange={setRunOpen} workflowId={workflow.id} onStarted={(id) => router.push(`/o/${slug}/automation/runs/${id}`)} />
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete "${workflow.name}"?`}
        description="Its triggers stop immediately. Run history stays in the audit log."
        confirmLabel="Delete workflow"
        confirmText={workflow.name}
        loading={del.isPending}
        onConfirm={async () => {
          try {
            await del.mutateAsync(workflow.id);
            toast.success("Workflow deleted");
            router.push(`/o/${slug}/automation`);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}

function RunDialog({ open, onOpenChange, workflowId, onStarted }: { open: boolean; onOpenChange: (o: boolean) => void; workflowId: string; onStarted: (runId: string) => void }) {
  const { slug } = useOrgContext();
  const start = useStartRun(slug, workflowId);
  const [input, setInput] = React.useState<unknown>(undefined);
  const [error, setError] = React.useState<string | null>(null);
  const submit = async () => {
    setError(null);
    if (input !== undefined && (typeof input !== "object" || input === null || Array.isArray(input))) return setError("The input must be a JSON object.");
    try {
      const run = await start.mutateAsync((input as Record<string, unknown>) ?? {});
      toast.success("Run started");
      onOpenChange(false);
      if (run.id) onStarted(run.id);
    } catch (e) {
      setError(errorMessage(e));
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader title="Run the published version" description="Steps run as you, with your permissions re-checked at every step." />
        <DialogBody className="space-y-2">
          <label htmlFor="run-input" className="text-xs font-medium">
            Input (optional JSON object, available as trigger.*)
          </label>
          <JsonInput id="run-input" value={undefined} onChange={setInput} rows={5} />
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
          <Button onClick={submit} loading={start.isPending}>
            <Play /> Run now
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
