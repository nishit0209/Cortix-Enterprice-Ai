"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Cable, Database, FileText, GitBranch, Layers, LayoutDashboard, Lock, Upload, Workflow, Brain, BookOpen } from "lucide-react";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { layeredLayout } from "@/lib/pipelines/dag";
import { isUnavailableEndpoint } from "@/lib/errors";
import { useLineage } from "@/lib/queries/pipelines";
import type { LineageGraph } from "@/lib/types";
import { cn, titleCase } from "@/lib/utils";

export const LINEAGE_TYPES: Record<string, { label: string; color: string; icon: React.ReactNode }> = {
  upload: { label: "Upload", color: "var(--chart-3)", icon: <Upload /> },
  data_source: { label: "Source", color: "var(--chart-5)", icon: <Cable /> },
  dataset: { label: "Dataset", color: "var(--chart-1)", icon: <Database /> },
  dataset_version: { label: "Version", color: "var(--chart-2)", icon: <Layers /> },
  pipeline: { label: "Pipeline", color: "var(--chart-7)", icon: <Workflow /> },
  pipeline_version: { label: "Pipeline version", color: "var(--chart-7)", icon: <Workflow /> },
  pipeline_run: { label: "Run", color: "var(--chart-7)", icon: <GitBranch /> },
  semantic_model: { label: "Semantic model", color: "var(--chart-4)", icon: <BookOpen /> },
  dashboard: { label: "Dashboard", color: "var(--chart-6)", icon: <LayoutDashboard /> },
  ml_model_version: { label: "Model", color: "var(--chart-8)", icon: <Brain /> },
  document: { label: "Document", color: "var(--chart-5)", icon: <FileText /> },
  restricted: { label: "Restricted", color: "var(--fg-subtle)", icon: <Lock /> },
};

type LNode = Node<{ label: string; kind: string; root: boolean }, "lineage">;

function LineageNodeView({ data }: NodeProps<LNode>) {
  const meta = LINEAGE_TYPES[data.kind] ?? { label: titleCase(data.kind), color: "var(--chart-8)", icon: <GitBranch /> };
  const restricted = data.kind === "restricted";
  return (
    <div
      className={cn(
        "flex w-[200px] items-center gap-2.5 rounded-card border bg-surface px-3 py-2 shadow-soft",
        restricted ? "border-dashed border-border-strong bg-surface-2 text-fg-muted" : data.root ? "border-accent ring-3 ring-accent/20" : "border-border",
      )}
      title={restricted ? "Assets you don't have access to" : data.label}
    >
      <Handle type="target" position={Position.Left} className="!size-2 !border-0 !bg-fg-subtle" isConnectable={false} />
      <span
        className="flex size-7 shrink-0 items-center justify-center rounded-control [&_svg]:size-3.5"
        style={{ background: `color-mix(in oklab, ${meta.color} 14%, transparent)`, color: meta.color }}
        aria-hidden
      >
        {meta.icon}
      </span>
      <div className="min-w-0">
        <div className="truncate text-[12.5px] font-semibold text-fg">{restricted ? "Restricted assets" : data.label}</div>
        <div className="truncate text-[10.5px] uppercase tracking-[0.06em] text-fg-subtle">{meta.label}</div>
      </div>
      <Handle type="source" position={Position.Right} className="!size-2 !border-0 !bg-fg-subtle" isConnectable={false} />
    </div>
  );
}

const nodeTypes = { lineage: LineageNodeView };

export function toLineageFlow(graph: LineageGraph, rootId: string | null): { nodes: LNode[]; edges: Edge[] } {
  const ids = graph.nodes.map((n) => n.id);
  const pos = layeredLayout(ids, graph.edges, { dx: 260, dy: 84 });
  return {
    nodes: graph.nodes.map((n) => ({
      id: n.id,
      type: "lineage" as const,
      position: pos[n.id] ?? { x: 0, y: 0 },
      data: { label: n.label, kind: n.type, root: n.id === rootId },
      draggable: true,
      connectable: false,
    })),
    edges: graph.edges.map((e, i) => ({
      id: `${e.from}->${e.to}-${i}`,
      source: e.from,
      target: e.to,
      label: e.label ?? undefined,
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      style: e.from === "restricted" || e.to === "restricted" ? { strokeDasharray: "4 4" } : undefined,
    })),
  };
}

/** Lineage graph around one asset (auto layout, colored by type, restricted assets collapsed). */
export function LineageGraphView({
  org,
  nodeType,
  nodeId,
  depth = 3,
  height = 520,
}: {
  org: string;
  nodeType: string;
  nodeId: string | null;
  depth?: number;
  height?: number;
}) {
  const q = useLineage(org, nodeType, nodeId, depth);
  const router = useRouter();
  const flow = React.useMemo(() => (q.data ? toLineageFlow(q.data, nodeId) : null), [q.data, nodeId]);
  if (!nodeId) return <EmptyState compact icon={<GitBranch />} title="Pick an asset" description="Choose a dataset to see where its data comes from and where it goes." />;
  if (q.isLoading) return <Skeleton className="w-full rounded-card" style={{ height }} />;
  if (q.isError) {
    return isUnavailableEndpoint(q.error) ? (
      <EmptyState compact icon={<GitBranch />} title="No lineage available" description="This asset has no recorded lineage you can see, or the lineage service isn't deployed." />
    ) : (
      <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
    );
  }
  if (!flow || flow.nodes.length === 0) return <EmptyState compact icon={<GitBranch />} title="No lineage yet" description="Lineage edges are recorded as data is ingested, transformed and published." />;
  const types = Array.from(new Set(flow.nodes.map((n) => n.data.kind)));
  return (
    <div className="space-y-2">
      <div className="overflow-hidden rounded-card border border-border" style={{ height }}>
        <ReactFlowProvider>
          <ReactFlow<LNode, Edge>
            nodes={flow.nodes}
            edges={flow.edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.2, maxZoom: 1.2 }}
            nodesConnectable={false}
            elementsSelectable
            proOptions={{ hideAttribution: true }}
            onNodeDoubleClick={(_, n) => {
              if (n.data.kind === "dataset") router.push(`/o/${org}/data/datasets/${n.id}`);
            }}
            className="nexora-flow"
          >
            <Background gap={20} size={1} color="var(--border-strong)" />
            <Controls showInteractive={false} position="bottom-left" />
          </ReactFlow>
        </ReactFlowProvider>
      </div>
      <ul className="flex flex-wrap items-center gap-3 text-xs text-fg-muted" aria-label="Legend">
        {types.map((t) => {
          const meta = LINEAGE_TYPES[t] ?? { label: titleCase(t), color: "var(--chart-8)" };
          return (
            <li key={t} className="flex items-center gap-1.5">
              <span className={cn("size-2.5 rounded-full", t === "restricted" && "border border-dashed border-fg-subtle")} style={t === "restricted" ? undefined : { background: meta.color }} aria-hidden />
              {meta.label}
            </li>
          );
        })}
        <li className="ml-auto text-fg-subtle">Double-click a dataset to open it.</li>
      </ul>
    </div>
  );
}
