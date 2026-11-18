"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import {
  addEdge,
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  ArrowLeft,
  CalendarClock,
  CheckCircle2,
  Eye,
  History,
  Loader2,
  Monitor,
  Play,
  Save,
  TriangleAlert,
  Workflow,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger, Tooltip } from "@/components/ui/overlays";
import { FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { JobProgress } from "@/components/jobs/job-progress";
import { ResultTable } from "@/components/ask/result-table";
import { api, isApiError, unwrapList } from "@/lib/api";
import { describeCron } from "@/lib/pipelines/cron";
import { CATEGORY_META, STEP_BY_TYPE, STEP_CATALOG, type StepCategory } from "@/lib/pipelines/catalog";
import {
  inferSchemas,
  newNodeId,
  parseDag,
  serializeDag,
  serverErrorsByNode,
  topoOrder,
  validateDag,
  type BuilderDag,
  type SchemaColumn,
} from "@/lib/pipelines/dag";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { datasetKeys, useDatasets } from "@/lib/queries/datasets";
import { deKeys, previewPipeline, runPipeline, usePipeline, useUpdatePipeline } from "@/lib/queries/pipelines";
import type { DatasetColumn, Paginated, Pipeline } from "@/lib/types";
import { cn } from "@/lib/utils";
import { BuilderContext, nodeTypes, STEP_ICONS, type StepNode } from "./step-node";
import { Inspector } from "./inspector";
import { ScheduleDialog } from "./schedule-dialog";

const DND_TYPE = "application/x-nexora-step";

function toFlow(dag: BuilderDag): { nodes: StepNode[]; edges: Edge[] } {
  return {
    nodes: dag.nodes.map((n) => ({
      id: n.id,
      type: "step" as const,
      position: n.position ?? { x: 0, y: 0 },
      data: { stepType: n.type, config: n.config },
    })),
    edges: dag.edges.map((e) => ({
      id: `${e.from}->${e.to}${e.port ? `:${e.port}` : ""}`,
      source: e.from,
      target: e.to,
      targetHandle: e.port ?? null,
      type: "smoothstep",
    })),
  };
}

function fromFlow(nodes: StepNode[], edges: Edge[], settings?: Record<string, unknown>): BuilderDag {
  return {
    nodes: nodes.map((n) => ({ id: n.id, type: n.data.stepType, config: n.data.config, position: n.position })),
    edges: edges.map((e) => ({
      from: e.source,
      to: e.target,
      ...(e.targetHandle === "left" || e.targetHandle === "right" ? { port: e.targetHandle } : {}),
    })),
    ...(settings ? { settings } : {}),
  };
}

function starterDag(): BuilderDag {
  return {
    nodes: [
      { id: "source_1", type: "source", config: { dataset_id: "" }, position: { x: 0, y: 80 } },
      { id: "output_1", type: "destination", config: { dataset_name: "", mode: "new_version" }, position: { x: 560, y: 80 } },
    ],
    edges: [],
  };
}

function useIsDesktop() {
  const [desktop, setDesktop] = React.useState(true);
  React.useEffect(() => {
    const mq = window.matchMedia("(min-width: 768px)");
    const on = () => setDesktop(mq.matches);
    on();
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return desktop;
}

export function PipelineBuilder() {
  const { id } = useParams<{ id: string }>();
  const { slug } = useOrgContext();
  const q = usePipeline(slug, id);
  useSetBreadcrumb(q.data?.name);
  if (q.isLoading) return <Skeleton className="h-[calc(100dvh-10rem)] rounded-card" />;
  if (q.isError || !q.data) {
    return (
      <Card>
        {isUnavailableEndpoint(q.error) ? (
          <EmptyState
            icon={<Workflow />}
            title="Pipeline not found"
            description="It doesn't exist, belongs to another organization, or the pipeline service isn't deployed yet."
            action={
              <Button variant="secondary" asChild>
                <Link href={`/o/${slug}/data/pipelines`}>
                  <ArrowLeft /> Pipelines
                </Link>
              </Button>
            }
          />
        ) : (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        )}
      </Card>
    );
  }
  return (
    <ReactFlowProvider>
      <BuilderInner key={q.data.id} pipeline={q.data} />
    </ReactFlowProvider>
  );
}

type BottomPanel =
  | { kind: "preview-job"; jobId: string; nodeId: string | null }
  | { kind: "preview"; columns: Array<{ name: string; type?: string } | string>; rows: unknown[][]; nodeId: string | null }
  | { kind: "run"; jobId: string }
  | null;

function BuilderInner({ pipeline }: { pipeline: Pipeline }) {
  const { slug, can } = useOrgContext();
  const qc = useQueryClient();
  const desktop = useIsDesktop();
  const readOnly = !can("pipelines.manage") || !desktop;
  const initial = React.useMemo(() => (pipeline.dag && (pipeline.dag.nodes ?? []).length ? parseDag(pipeline.dag) : starterDag()), [pipeline.dag]);
  const flow0 = React.useMemo(() => toFlow(initial), [initial]);
  const [nodes, setNodes, onNodesChange] = useNodesState<StepNode>(flow0.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(flow0.edges);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [name, setName] = React.useState(pipeline.name);
  const [serverIssues, setServerIssues] = React.useState<Record<string, string[]>>({});
  const [savedJson, setSavedJson] = React.useState(() => JSON.stringify(serializeDag(initial)));
  const [panel, setPanel] = React.useState<BottomPanel>(null);
  const [scheduleOpen, setScheduleOpen] = React.useState(false);
  const [busy, setBusy] = React.useState<"preview" | "run" | null>(null);
  const update = useUpdatePipeline(slug, pipeline.id);
  const rf = useReactFlow<StepNode, Edge>();
  const datasets = useDatasets(slug);

  const dag = React.useMemo(() => fromFlow(nodes, edges, initial.settings), [nodes, edges, initial.settings]);
  const json = JSON.stringify(serializeDag(dag));
  const dirty = json !== savedJson || name.trim() !== pipeline.name;

  // Source dataset columns → static schema inference.
  const sourceIds = Array.from(new Set(dag.nodes.filter((n) => n.type === "source" && n.config.dataset_id).map((n) => String(n.config.dataset_id))));
  const colQueries = useQueries({
    queries: sourceIds.map((dsId) => ({
      queryKey: datasetKeys.columns(slug, dsId),
      queryFn: async ({ signal }: { signal: AbortSignal }) =>
        unwrapList(await api.get<DatasetColumn[] | Paginated<DatasetColumn>>(`/orgs/${encodeURIComponent(slug)}/datasets/${dsId}/columns`, { signal })),
      staleTime: 60_000,
    })),
  });
  const colKey = colQueries.map((c) => c.dataUpdatedAt).join(",");
  const sourceColumns = React.useMemo(() => {
    const m: Record<string, SchemaColumn[] | undefined> = {};
    sourceIds.forEach((dsId, i) => {
      const d = colQueries[i]?.data;
      if (d) m[dsId] = d.map((c) => ({ name: c.name, type: c.data_type }));
    });
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceIds.join(","), colKey]);
  const schemas = React.useMemo(() => inferSchemas(dag, sourceColumns), [dag, sourceColumns]);
  const validation = React.useMemo(() => validateDag(dag, schemas), [dag, schemas]);
  const issues = React.useMemo(() => {
    const merged: Record<string, string[]> = { ...validation.byNode };
    for (const [k, v] of Object.entries(serverIssues)) merged[k] = [...(merged[k] ?? []), ...v];
    return merged;
  }, [validation.byNode, serverIssues]);
  const globalIssues = validation.issues.filter((i) => !i.nodeId);

  const datasetName = React.useCallback((dsId: unknown) => (datasets.data ?? []).find((d) => d.id === dsId)?.name, [datasets.data]);
  const summary = React.useCallback(
    (_id: string, type: string, c: Record<string, unknown>) => {
      const arr = (k: string) => (Array.isArray(c[k]) ? (c[k] as unknown[]) : []);
      switch (type) {
        case "source":
          return datasetName(c.dataset_id) ?? (c.dataset_id ? "Dataset" : "Pick a dataset");
        case "destination":
          return c.dataset_name ? `→ ${String(c.dataset_name)}` : "Name the output";
        case "select_columns":
        case "trim_whitespace":
        case "drop_nulls":
        case "fill_null":
        case "change_case":
          return arr("columns").length ? arr("columns").join(", ") : "";
        case "rename":
          return Object.entries((c.mapping ?? {}) as Record<string, string>).map(([a, b]) => `${a}→${b}`).join(", ");
        case "deduplicate":
          return arr("keys").length ? `by ${arr("keys").join(", ")}` : "whole rows";
        case "filter":
          return `${arr("conditions").length} condition${arr("conditions").length === 1 ? "" : "s"}`;
        case "derive":
          return c.name ? `${String(c.name)} = ${String(c.expression ?? "")}` : "";
        case "sort":
          return (arr("by") as Array<{ column: string; desc?: boolean }>).map((s) => `${s.column}${s.desc ? " ↓" : ""}`).join(", ");
        case "aggregate":
          return arr("group_by").length ? `by ${arr("group_by").join(", ")}` : `${arr("aggregations").length} aggregates`;
        case "join":
          return `${String(c.how ?? "inner")} join`;
        case "cast":
          return (arr("casts") as Array<{ column: string; to: string }>).map((x) => `${x.column}::${x.to}`).join(", ");
        default:
          return "";
      }
    },
    [datasetName],
  );

  const ctx = React.useMemo(() => ({ issues, summary, readOnly }), [issues, summary, readOnly]);

  const isValidConnection = React.useCallback(
    (conn: Connection | Edge) => {
      if (!conn.source || !conn.target || conn.source === conn.target) return false;
      const target = nodes.find((n) => n.id === conn.target);
      const spec = target ? STEP_BY_TYPE[target.data.stepType] : undefined;
      if (!spec || spec.inputs === 0) return false;
      const existing = edges.filter((e) => e.target === conn.target);
      if (spec.inputs === 1 && existing.length >= 1) return false;
      if (spec.inputs === 2 && existing.some((e) => e.targetHandle === conn.targetHandle)) return false;
      const { cyclic } = topoOrder(
        nodes.map((n) => n.id),
        [...edges.map((e) => ({ from: e.source, to: e.target })), { from: conn.source, to: conn.target }],
      );
      return cyclic.length === 0;
    },
    [nodes, edges],
  );

  const onConnect = React.useCallback(
    (conn: Connection) => {
      setEdges((eds) => addEdge({ ...conn, id: `${conn.source}->${conn.target}${conn.targetHandle ? `:${conn.targetHandle}` : ""}`, type: "smoothstep" }, eds));
      setServerIssues({});
    },
    [setEdges],
  );

  const addStep = React.useCallback(
    (type: string, position?: { x: number; y: number }) => {
      const spec = STEP_BY_TYPE[type];
      if (!spec) return;
      const id = newNodeId(type, nodes.map((n) => n.id));
      const sel = nodes.find((n) => n.id === selected);
      const pos =
        position ??
        (sel
          ? { x: sel.position.x + 280, y: sel.position.y + 20 }
          : rf.screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 }));
      setNodes((ns) => [...ns, { id, type: "step", position: pos, data: { stepType: type, config: structuredClone(spec.defaults) } }]);
      // Auto-wire after the selected node when it has a free output and the new step takes one input.
      if (!position && sel && STEP_BY_TYPE[sel.data.stepType]?.outputs && spec.inputs === 1) {
        setEdges((eds) => [...eds, { id: `${sel.id}->${id}`, source: sel.id, target: id, type: "smoothstep" }]);
      }
      setSelected(id);
    },
    [nodes, selected, rf, setNodes, setEdges],
  );

  const updateConfig = (id: string, config: Record<string, unknown>) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, config } } : n)));
    setServerIssues((s) => {
      if (!s[id]) return s;
      const next = { ...s };
      delete next[id];
      return next;
    });
  };

  const deleteNode = (id: string) => {
    setNodes((ns) => ns.filter((n) => n.id !== id));
    setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
    setSelected(null);
  };

  const save = async (): Promise<boolean> => {
    try {
      const body: { dag: ReturnType<typeof serializeDag>; name?: string } = { dag: serializeDag(dag) };
      if (name.trim() && name.trim() !== pipeline.name) body.name = name.trim();
      const saved = await update.mutateAsync(body);
      setSavedJson(json);
      setServerIssues({});
      toast.success(saved?.version ? `Saved version ${saved.version}` : "Pipeline saved");
      return true;
    } catch (e) {
      if (isApiError(e) && e.errors.length) {
        const mapped = serverErrorsByNode(e.errors, dag);
        setServerIssues(mapped);
        const unmapped = e.errors.filter((x) => !x.field || !/nodes/.test(x.field));
        toast.error(unmapped.length ? errorMessage(e) : "The server rejected some steps. See the highlighted nodes.");
      } else toast.error(errorMessage(e));
      return false;
    }
  };

  const preview = async () => {
    setBusy("preview");
    try {
      const res = await previewPipeline(slug, pipeline.id, { dag: serializeDag(dag), node_id: selected ?? undefined, limit: 50 });
      if ("job_id" in res && res.job_id) setPanel({ kind: "preview-job", jobId: res.job_id, nodeId: selected });
      else {
        const r = res as { columns: Array<{ name: string; type?: string } | string>; rows: unknown[][]; errors?: Array<{ field: string | null; message: string }> };
        if (r.errors?.length) setServerIssues(serverErrorsByNode(r.errors, dag));
        setPanel({ kind: "preview", columns: r.columns ?? [], rows: r.rows ?? [], nodeId: selected });
      }
    } catch (e) {
      if (isApiError(e) && e.errors.length) setServerIssues(serverErrorsByNode(e.errors, dag));
      toast.error(isUnavailableEndpoint(e) ? "Preview isn't available on this backend yet." : errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const run = async () => {
    if (!validation.ok) {
      toast.error("Fix the highlighted issues before running.");
      return;
    }
    setBusy("run");
    try {
      if (dirty && !(await save())) return;
      const res = await runPipeline(slug, pipeline.id);
      setPanel({ kind: "run", jobId: res.job_id });
      void qc.invalidateQueries({ queryKey: deKeys.runs(slug, pipeline.id) });
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  // Ctrl/Cmd+S saves.
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (!readOnly && dirty) void save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const selNode = nodes.find((n) => n.id === selected) ?? null;
  const selInputs = selNode ? dag.edges.filter((e) => e.to === selNode.id) : [];
  const leftIn = selInputs.find((e) => e.port === "left") ?? selInputs[0];
  const rightIn = selInputs.find((e) => e !== leftIn);
  const issueCount = Object.values(issues).reduce((a, v) => a + v.length, 0) + globalIssues.length;
  const grouped = (Object.keys(CATEGORY_META) as StepCategory[]).map((cat) => ({ cat, steps: STEP_CATALOG.filter((s) => s.category === cat) }));

  return (
    <BuilderContext.Provider value={ctx}>
      <div className="-mx-4 -my-6 flex h-[calc(100dvh-3.5rem)] min-h-[560px] flex-col sm:-mx-6 lg:-mx-8 lg:-my-8">
        {/* Top bar */}
        <div className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-3 py-2 sm:px-4">
          <Button variant="ghost" size="icon-sm" asChild>
            <Link href={`/o/${slug}/data/pipelines`} aria-label="Back to pipelines">
              <ArrowLeft />
            </Link>
          </Button>
          <Input
            aria-label="Pipeline name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={readOnly}
            className="h-8 w-48 border-transparent bg-transparent font-semibold shadow-none hover:border-border sm:w-64"
          />
          <StatusBadge status={pipeline.status} />
          <FeatureStatusBadge status="prototype" className="hidden md:inline-flex" />
          {dirty && !readOnly && <Badge tone="warning">Unsaved</Badge>}
          <Popover>
            <PopoverTrigger asChild>
              <button
                type="button"
                className={cn("inline-flex h-7 items-center gap-1.5 rounded-full px-2.5 text-xs font-medium focus-ring", issueCount ? "bg-danger-soft text-danger" : "bg-success-soft text-success")}
              >
                {issueCount ? <TriangleAlert className="size-3.5" /> : <CheckCircle2 className="size-3.5" />}
                {issueCount ? `${issueCount} issue${issueCount === 1 ? "" : "s"}` : "Valid"}
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" className="w-80 p-0">
              <ul className="scrollbar-thin max-h-72 divide-y divide-border overflow-y-auto text-sm">
                {issueCount === 0 && <li className="px-4 py-3 text-fg-muted">The DAG is valid: acyclic, one output and every step configured.</li>}
                {globalIssues.map((i) => (
                  <li key={i.message} className="px-4 py-2.5 text-fg">
                    {i.message}
                  </li>
                ))}
                {Object.entries(issues).flatMap(([nid, msgs]) =>
                  msgs.map((m) => (
                    <li key={`${nid}:${m}`}>
                      <button type="button" className="w-full px-4 py-2.5 text-left hover:bg-surface-2 focus-ring" onClick={() => {
                        setSelected(nid);
                        const n = nodes.find((x) => x.id === nid);
                        if (n) void rf.setCenter(n.position.x + 110, n.position.y + 30, { zoom: 1.1, duration: 300 });
                      }}>
                        <span className="font-mono text-xs text-fg-subtle">{nid}</span>
                        <span className="block text-fg">{m}</span>
                      </button>
                    </li>
                  )),
                )}
              </ul>
            </PopoverContent>
          </Popover>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            {pipeline.schedule && (
              <Tooltip content={describeCron(pipeline.schedule.cron, pipeline.schedule.timezone)}>
                <span className="hidden xl:inline">
                  <Badge tone={pipeline.schedule.is_active === false ? "outline" : "info"}>
                    <CalendarClock /> {pipeline.schedule.is_active === false ? "Paused" : "Scheduled"}
                  </Badge>
                </span>
              </Tooltip>
            )}
            <Button variant="ghost" size="sm" asChild>
              <Link href={`/o/${slug}/data/pipelines/${pipeline.id}/runs`}>
                <History /> Runs
              </Link>
            </Button>
            {can("pipelines.manage") && (
              <Button variant="secondary" size="sm" onClick={() => setScheduleOpen(true)}>
                <CalendarClock /> Schedule
              </Button>
            )}
            <Button variant="secondary" size="sm" onClick={() => void preview()} loading={busy === "preview"} disabled={dag.nodes.length === 0}>
              <Eye /> Preview{selNode ? " step" : ""}
            </Button>
            {!readOnly && (
              <Button variant="secondary" size="sm" onClick={() => void save()} loading={update.isPending && busy !== "run"} disabled={!dirty}>
                <Save /> Save version
              </Button>
            )}
            {can("pipelines.run") && (
              <Button size="sm" onClick={() => void run()} loading={busy === "run"}>
                <Play /> Run now
              </Button>
            )}
          </div>
        </div>
        {!desktop && (
          <div className="flex items-center gap-2 border-b border-border bg-info-soft px-4 py-2 text-xs text-info">
            <Monitor className="size-3.5" aria-hidden /> Pipeline editing is best on desktop. The canvas is read-only on small screens.
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          {/* Palette */}
          {!readOnly && (
            <aside className="scrollbar-thin hidden w-56 shrink-0 overflow-y-auto border-r border-border bg-surface-2/40 p-3 lg:block" aria-label="Step palette">
              <p className="mb-2 text-xs text-fg-muted">Drag onto the canvas, or click to add after the selected step.</p>
              {grouped.map(({ cat, steps }) => (
                <div key={cat} className="mb-3">
                  <div className="mb-1 flex items-center gap-1.5 px-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-subtle">
                    <span className="size-1.5 rounded-full" style={{ background: CATEGORY_META[cat].color }} aria-hidden />
                    {CATEGORY_META[cat].label}
                  </div>
                  <ul className="space-y-0.5">
                    {steps.map((s) => (
                      <li key={s.type}>
                        <button
                          type="button"
                          draggable
                          onDragStart={(e) => {
                            e.dataTransfer.setData(DND_TYPE, s.type);
                            e.dataTransfer.effectAllowed = "move";
                          }}
                          onClick={() => addStep(s.type)}
                          title={s.description}
                          className="flex w-full cursor-grab items-center gap-2 rounded-control px-2 py-1.5 text-left text-sm text-fg transition-colors hover:bg-surface-3 focus-ring active:cursor-grabbing [&_svg]:size-3.5"
                        >
                          <span style={{ color: CATEGORY_META[cat].color }}>{STEP_ICONS[s.type]}</span>
                          {s.label}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </aside>
          )}

          {/* Canvas */}
          <div
            className="relative min-w-0 flex-1"
            onDragOver={(e) => {
              if (e.dataTransfer.types.includes(DND_TYPE)) {
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
              }
            }}
            onDrop={(e) => {
              const type = e.dataTransfer.getData(DND_TYPE);
              if (!type) return;
              e.preventDefault();
              addStep(type, rf.screenToFlowPosition({ x: e.clientX - 110, y: e.clientY - 28 }));
            }}
          >
            <ReactFlow<StepNode, Edge>
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={readOnly ? undefined : onNodesChange}
              onEdgesChange={readOnly ? undefined : onEdgesChange}
              onConnect={readOnly ? undefined : onConnect}
              isValidConnection={isValidConnection}
              onNodeClick={(_, n) => setSelected(n.id)}
              onPaneClick={() => setSelected(null)}
              nodesDraggable={!readOnly}
              nodesConnectable={!readOnly}
              elementsSelectable
              deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
              onNodesDelete={(del) => del.some((d) => d.id === selected) && setSelected(null)}
              fitView
              fitViewOptions={{ padding: 0.25, maxZoom: 1.1 }}
              defaultEdgeOptions={{ type: "smoothstep" }}
              proOptions={{ hideAttribution: true }}
              className="nexora-flow"
            >
              <Background gap={20} size={1} color="var(--border-strong)" />
              <Controls showInteractive={false} position="bottom-left" />
              <MiniMap pannable zoomable position="bottom-right" nodeColor={(n) => CATEGORY_META[STEP_BY_TYPE[(n.data as StepNode["data"]).stepType]?.category ?? "reshape"].color} maskColor="color-mix(in oklab, var(--bg) 70%, transparent)" />
            </ReactFlow>
            {dag.nodes.length === 0 && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                <EmptyState compact icon={<Workflow />} title="Empty pipeline" description="Add a source, transformation steps and an output from the palette." />
              </div>
            )}
          </div>

          {/* Inspector */}
          {selNode && (
            <aside className="w-[340px] shrink-0 border-l border-border bg-surface" aria-label="Step settings">
              <Inspector
                key={selNode.id}
                nodeId={selNode.id}
                stepType={selNode.data.stepType}
                config={selNode.data.config}
                onChange={(c) => updateConfig(selNode.id, c)}
                onDelete={() => deleteNode(selNode.id)}
                input={leftIn ? (schemas[leftIn.from] ?? null) : null}
                rightInput={rightIn ? (schemas[rightIn.from] ?? null) : null}
                output={schemas[selNode.id] ?? null}
                issues={issues[selNode.id] ?? []}
                datasets={datasets.data ?? []}
                readOnly={readOnly}
              />
            </aside>
          )}
        </div>

        {/* Bottom panel: preview / live run */}
        {panel && (
          <div className="max-h-[42%] min-h-[180px] overflow-hidden border-t border-border bg-surface">
            <div className="flex items-center gap-2 border-b border-border px-4 py-2 text-sm">
              {panel.kind === "run" ? <Play className="size-4 text-accent" /> : <Eye className="size-4 text-accent" />}
              <span className="font-medium text-fg">{panel.kind === "run" ? "Run" : `Preview${panel.nodeId ? ` · ${panel.nodeId}` : ""}`}</span>
              {panel.kind === "run" && (
                <Button variant="link" size="xs" asChild className="ml-2">
                  <Link href={`/o/${slug}/data/pipelines/${pipeline.id}/runs`}>Open run history</Link>
                </Button>
              )}
              <Button variant="ghost" size="icon-xs" className="ml-auto" onClick={() => setPanel(null)} aria-label="Close panel">
                <X />
              </Button>
            </div>
            <div className="scrollbar-thin h-full overflow-y-auto p-4 pb-14">
              {panel.kind === "run" ? (
                <JobProgress
                  org={slug}
                  jobId={panel.jobId}
                  title="Pipeline run"
                  onDone={(s) => {
                    void qc.invalidateQueries({ queryKey: deKeys.runs(slug, pipeline.id) });
                    void qc.invalidateQueries({ queryKey: deKeys.pipelines(slug) });
                    if (s.status === "succeeded") toast.success("Pipeline run succeeded");
                  }}
                />
              ) : panel.kind === "preview-job" ? (
                <JobProgress
                  org={slug}
                  jobId={panel.jobId}
                  title="Dry run"
                  showLogs={false}
                  onDone={(s) => {
                    const r = (s.result ?? {}) as { preview?: { columns: Array<{ name: string } | string>; rows: unknown[][] }; columns?: Array<{ name: string } | string>; rows?: unknown[][] };
                    const p = r.preview ?? (r.columns ? { columns: r.columns, rows: r.rows ?? [] } : null);
                    if (p) setPanel({ kind: "preview", columns: p.columns, rows: p.rows, nodeId: panel.nodeId });
                    else if (s.status !== "succeeded") toast.error(s.error?.message ?? "Dry run failed");
                  }}
                />
              ) : panel.columns.length === 0 ? (
                <p className="text-sm text-fg-muted">The dry run produced no columns.</p>
              ) : (
                <ResultTable columns={panel.columns} rows={panel.rows} caption="Pipeline preview" />
              )}
            </div>
          </div>
        )}
        {busy === "run" && (
          <div className="sr-only" role="status">
            <Loader2 /> Starting run…
          </div>
        )}
      </div>
      <ScheduleDialog org={slug} pipelineId={pipeline.id} schedule={pipeline.schedule} open={scheduleOpen} onOpenChange={setScheduleOpen} />
    </BuilderContext.Provider>
  );
}
