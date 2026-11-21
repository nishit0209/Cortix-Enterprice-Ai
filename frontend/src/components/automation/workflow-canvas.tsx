"use client";

/**
 * The workflow canvas (@xyflow/react). Loaded with next/dynamic from the editor, so the flow library only
 * downloads on the builder route.
 */

import * as React from "react";
import {
  addEdge,
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ShieldAlert, TriangleAlert } from "lucide-react";
import { stepKind, type StepKind } from "@/lib/automation/catalog";
import { newStepId, topoOrder, type WfGraph } from "@/lib/automation/graph";
import { cn } from "@/lib/utils";
import { DND_TYPE, stepIcon } from "./workflow-canvas-shared";

export interface WfNodeData extends Record<string, unknown> {
  kind: string;
  name: string;
  config: Record<string, unknown>;
}
export type WfFlowNode = Node<WfNodeData, "wf">;

const CanvasContext = React.createContext<{ catalog: StepKind[]; issues: Record<string, string[]>; readOnly: boolean }>({ catalog: [], issues: {}, readOnly: true });

const handleCls = "!size-2.5 !border-2 !border-surface !bg-fg-subtle hover:!bg-accent";

function WfNodeView({ id, data, selected }: NodeProps<WfFlowNode>) {
  const ctx = React.useContext(CanvasContext);
  const k = stepKind(ctx.catalog, data.kind);
  const issues = ctx.issues[id] ?? [];
  return (
    <div
      className={cn(
        "relative w-[210px] rounded-card border bg-surface text-left shadow-soft",
        selected ? "border-accent ring-3 ring-accent/20" : issues.length ? "border-danger/60" : "border-border hover:border-border-strong",
      )}
    >
      <Handle type="target" position={Position.Left} className={handleCls} isConnectable={!ctx.readOnly} />
      <div className="flex items-start gap-2.5 px-3 py-2.5">
        <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-control bg-accent-soft text-accent-soft-fg [&_svg]:size-3.5" aria-hidden>
          {stepIcon(k)}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-fg">{data.name || k.label}</div>
          <div className="truncate font-mono text-[10px] text-fg-muted">{data.kind}</div>
        </div>
        {k.impact === "high" && <ShieldAlert className="size-3.5 shrink-0 text-warning" aria-label="High impact" />}
        {issues.length > 0 && (
          <span className="flex items-center gap-0.5 rounded-full bg-danger-soft px-1.5 py-0.5 text-[10px] font-semibold text-danger" title={issues.join("\n")}>
            <TriangleAlert className="size-3" aria-hidden />
            {issues.length}
          </span>
        )}
      </div>
      {data.kind === "condition" ? (
        <>
          <Handle id="true" type="source" position={Position.Right} style={{ top: "35%" }} className={cn(handleCls, "!bg-success")} isConnectable={!ctx.readOnly} title="Yes" />
          <Handle id="false" type="source" position={Position.Right} style={{ top: "70%" }} className={cn(handleCls, "!bg-danger")} isConnectable={!ctx.readOnly} title="No" />
          <span className="pointer-events-none absolute -right-7 top-[22%] text-[9px] font-semibold text-success">yes</span>
          <span className="pointer-events-none absolute -right-6 top-[60%] text-[9px] font-semibold text-danger">no</span>
        </>
      ) : (
        <Handle type="source" position={Position.Right} className={handleCls} isConnectable={!ctx.readOnly} />
      )}
    </div>
  );
}

const nodeTypes = { wf: WfNodeView };

function toFlow(g: WfGraph): { nodes: WfFlowNode[]; edges: Edge[] } {
  return {
    nodes: g.nodes.map((n) => ({ id: n.id, type: "wf" as const, position: n.position, data: { kind: n.kind, name: n.name, config: n.config } })),
    edges: g.edges.map((e) => ({
      id: `${e.from}->${e.to}${e.branch ? `:${e.branch}` : ""}`,
      source: e.from,
      target: e.to,
      sourceHandle: e.branch ?? null,
      type: "smoothstep",
      label: e.branch === "true" ? "yes" : e.branch === "false" ? "no" : undefined,
    })),
  };
}

export function fromFlow(nodes: WfFlowNode[], edges: Edge[]): WfGraph {
  return {
    nodes: nodes.map((n) => ({ id: n.id, kind: n.data.kind, name: n.data.name, config: n.data.config, position: n.position })),
    edges: edges.map((e) => ({ from: e.source, to: e.target, ...(e.sourceHandle === "true" || e.sourceHandle === "false" ? { branch: e.sourceHandle } : {}) })),
  };
}

export interface CanvasApi {
  addStep: (kind: StepKind) => void;
  updateNode: (id: string, patch: Partial<WfNodeData>) => void;
  deleteNode: (id: string) => void;
}

function CanvasInner({
  initial,
  catalog,
  issues,
  readOnly,
  onChange,
  selected,
  onSelect,
  apiRef,
}: {
  initial: WfGraph;
  catalog: StepKind[];
  issues: Record<string, string[]>;
  readOnly: boolean;
  onChange: (g: WfGraph) => void;
  selected: string | null;
  onSelect: (id: string | null) => void;
  apiRef: React.MutableRefObject<CanvasApi | null>;
}) {
  const flow0 = React.useMemo(() => toFlow(initial), [initial]);
  const [nodes, setNodes, onNodesChange] = useNodesState<WfFlowNode>(flow0.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(flow0.edges);
  const rf = useReactFlow<WfFlowNode, Edge>();

  React.useEffect(() => {
    onChange(fromFlow(nodes, edges));
  }, [nodes, edges, onChange]);

  const add = React.useCallback(
    (k: StepKind, position?: { x: number; y: number }) => {
      const id = newStepId(k.kind, nodes.map((n) => n.id));
      const sel = nodes.find((n) => n.id === selected);
      const last = nodes[nodes.length - 1];
      const pos = position ?? (sel ? { x: sel.position.x + 260, y: sel.position.y } : last ? { x: last.position.x + 260, y: last.position.y } : { x: 40, y: 80 });
      const defaults: Record<string, unknown> = {};
      for (const [key, p] of Object.entries(k.schema.properties ?? {})) if (p.default !== undefined) defaults[key] = p.default;
      setNodes((ns) => [...ns, { id, type: "wf", position: pos, data: { kind: k.kind, name: "", config: defaults } }]);
      const from = sel ?? (position ? null : last);
      if (from && from.data.kind !== "condition") setEdges((es) => [...es, { id: `${from.id}->${id}`, source: from.id, target: id, type: "smoothstep" }]);
      onSelect(id);
    },
    [nodes, selected, setNodes, setEdges, onSelect],
  );

  React.useEffect(() => {
    apiRef.current = {
    addStep: (k) => add(k),
    updateNode: (id, patch) => setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n))),
    deleteNode: (id) => {
      setNodes((ns) => ns.filter((n) => n.id !== id));
      setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
      onSelect(null);
    },
  };
  });

  const isValidConnection = React.useCallback(
    (c: Connection | Edge) => {
      if (!c.source || !c.target || c.source === c.target) return false;
      if (edges.some((e) => e.target === c.target)) return false; // one incoming connection per step
      const g = fromFlow(nodes, [...edges, { id: "x", source: c.source, target: c.target, sourceHandle: c.sourceHandle ?? null }]);
      return topoOrder(g) !== null;
    },
    [nodes, edges],
  );

  const ctx = React.useMemo(() => ({ catalog, issues, readOnly }), [catalog, issues, readOnly]);

  return (
    <CanvasContext.Provider value={ctx}>
      <div
        className="h-full w-full"
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes(DND_TYPE)) {
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
          }
        }}
        onDrop={(e) => {
          const kind = e.dataTransfer.getData(DND_TYPE);
          if (!kind || readOnly) return;
          e.preventDefault();
          add(stepKind(catalog, kind), rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
        }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={readOnly ? undefined : onNodesChange}
          onEdgesChange={readOnly ? undefined : onEdgesChange}
          onConnect={(c) => setEdges((es) => addEdge({ ...c, id: `${c.source}->${c.target}${c.sourceHandle ? `:${c.sourceHandle}` : ""}`, type: "smoothstep", label: c.sourceHandle === "true" ? "yes" : c.sourceHandle === "false" ? "no" : undefined }, es))}
          isValidConnection={isValidConnection}
          onSelectionChange={({ nodes: sel }) => onSelect(sel[0]?.id ?? null)}
          onPaneClick={() => onSelect(null)}
          nodesDraggable={!readOnly}
          nodesConnectable={!readOnly}
          deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
          fitView
          fitViewOptions={{ padding: 0.3, maxZoom: 1.2 }}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={20} size={1} />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable className="!hidden md:!block" />
        </ReactFlow>
      </div>
    </CanvasContext.Provider>
  );
}

export default function WorkflowCanvas(props: React.ComponentProps<typeof CanvasInner>) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
