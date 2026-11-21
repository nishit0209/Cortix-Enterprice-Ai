"use client";

/**
 * Automation and agents (API_SPECIFICATION Phase 10, CONTRACT "W5 additions"). Payloads are normalized by
 * `lib/automation/graph.ts`; endpoints that this server doesn't provide surface as "not available" (404/405/501).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fetchAll } from "@/lib/api";
import { isUnavailableEndpoint } from "@/lib/errors";
import { mergeCatalog } from "@/lib/automation/catalog";
import { graphToWire, mapAgent, mapApproval, mapRun, mapStepRun, mapTrigger, mapVersion, mapWorkflow, type WfGraph, type WorkflowRun } from "@/lib/automation/graph";
import { listOf } from "@/lib/ml/mappers";
import { useCursorList } from "./paged";

const base = (org: string) => `/orgs/${encodeURIComponent(org)}`;
const retry = (n: number, e: unknown) => !isUnavailableEndpoint(e) && n < 1;
const ACTIVE_RUN = new Set(["queued", "running", "waiting_approval", "retrying"]);

export const autoKeys = {
  all: (org: string) => ["org", org, "automation"] as const,
  steps: (org: string) => ["org", org, "automation", "steps"] as const,
  workflows: (org: string) => ["org", org, "automation", "workflows"] as const,
  workflow: (org: string, id: string) => ["org", org, "automation", "workflows", id] as const,
  versions: (org: string, id: string) => ["org", org, "automation", "workflows", id, "versions"] as const,
  triggers: (org: string, id: string) => ["org", org, "automation", "workflows", id, "triggers"] as const,
  runs: (org: string, workflowId: string | null) => ["org", org, "automation", "runs", workflowId ?? "all"] as const,
  run: (org: string, id: string) => ["org", org, "automation", "run", id] as const,
  steps_of: (org: string, id: string) => ["org", org, "automation", "run", id, "steps"] as const,
  approvals: (org: string) => ["org", org, "automation", "approvals"] as const,
  agents: (org: string) => ["org", org, "automation", "agents"] as const,
};

/** The registered step kinds; falls back to the CONTRACT list when the registry isn't exposed. */
export function useStepCatalog(org: string) {
  return useQuery({
    queryKey: autoKeys.steps(org),
    staleTime: 5 * 60_000,
    retry: false,
    queryFn: async ({ signal }) => {
      for (const path of ["/automation/steps", "/workflow-steps"]) {
        try {
          return { steps: mergeCatalog(await api.get<unknown>(`${base(org)}${path}`, { signal })), fromServer: true };
        } catch (e) {
          if (!isUnavailableEndpoint(e)) throw e;
        }
      }
      return { steps: mergeCatalog(null), fromServer: false };
    },
  });
}

export function useWorkflows(org: string, enabled = true) {
  return useQuery({ queryKey: autoKeys.workflows(org), enabled, retry, queryFn: async ({ signal }) => (await fetchAll<unknown>(`${base(org)}/workflows`, { signal })).map(mapWorkflow) });
}

export function useWorkflow(org: string, id: string) {
  return useQuery({ queryKey: autoKeys.workflow(org, id), retry, queryFn: async ({ signal }) => mapWorkflow(await api.get<unknown>(`${base(org)}/workflows/${id}`, { signal })) });
}

export function useWorkflowVersions(org: string, id: string) {
  return useQuery({
    queryKey: autoKeys.versions(org, id),
    retry,
    queryFn: async ({ signal }) =>
      listOf(await api.get<unknown>(`${base(org)}/workflows/${id}/versions`, { signal, query: { limit: 100 } }), "versions")
        .map(mapVersion)
        .sort((a, b) => b.version - a.version),
  });
}

export function useTriggers(org: string, id: string) {
  return useQuery({
    queryKey: autoKeys.triggers(org, id),
    retry,
    queryFn: async ({ signal }) => listOf(await api.get<unknown>(`${base(org)}/workflows/${id}/triggers`, { signal }), "triggers").map(mapTrigger),
  });
}

/** Runs, org-wide or of one workflow. Polls while any run is active. */
export function useWorkflowRuns(org: string, workflowId: string | null, enabled = true) {
  const list = useCursorList(autoKeys.runs(org, workflowId), `${base(org)}/workflow-runs`, {
    query: { workflow_id: workflowId ?? undefined },
    limit: 25,
    enabled,
    map: mapRun,
  });
  return list;
}

export function useWorkflowRun(org: string, id: string) {
  return useQuery<WorkflowRun>({
    queryKey: autoKeys.run(org, id),
    retry,
    refetchInterval: (q) => (q.state.data && ACTIVE_RUN.has(q.state.data.status) ? 3000 : false),
    queryFn: async ({ signal }) => mapRun(await api.get<unknown>(`${base(org)}/workflow-runs/${id}`, { signal })),
  });
}

export function useRunSteps(org: string, id: string, live: boolean) {
  return useQuery({
    queryKey: autoKeys.steps_of(org, id),
    retry,
    refetchInterval: live ? 3000 : false,
    queryFn: async ({ signal }) => listOf(await api.get<unknown>(`${base(org)}/workflow-runs/${id}/steps`, { signal }), "steps").map(mapStepRun),
  });
}

export function useApprovals(org: string, status: string, enabled = true) {
  return useCursorList(autoKeys.approvals(org), `${base(org)}/approvals`, { query: { status: status || undefined }, limit: 25, enabled, map: mapApproval });
}

export function useAgents(org: string, enabled = true) {
  return useQuery({ queryKey: autoKeys.agents(org), enabled, retry, queryFn: async ({ signal }) => (await fetchAll<unknown>(`${base(org)}/agents`, { signal })).map(mapAgent) });
}

function useInvalidate(org: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: autoKeys.all(org) });
}

export function useCreateWorkflow(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async (body: { name: string; description?: string; graph?: WfGraph }) =>
      mapWorkflow(await api.post<unknown>(`${base(org)}/workflows`, { name: body.name, description: body.description, ...(body.graph ? { graph: graphToWire(body.graph) } : {}) })),
    onSuccess: inv,
  });
}

export function useUpdateWorkflow(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async (body: { name?: string; description?: string; status?: string }) => mapWorkflow(await api.patch<unknown>(`${base(org)}/workflows/${id}`, body)),
    onSuccess: inv,
  });
}

export function useDeleteWorkflow(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (id: string) => api.delete(`${base(org)}/workflows/${id}`), onSuccess: inv });
}

/** Saves the canvas as a new (draft) version. */
export function useSaveVersion(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async (graph: WfGraph) => mapVersion(await api.post<unknown>(`${base(org)}/workflows/${id}/versions`, { graph: graphToWire(graph) })),
    onSuccess: inv,
  });
}

export function usePublishWorkflow(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (version?: number) => api.post<unknown>(`${base(org)}/workflows/${id}/publish`, version ? { version } : {}), onSuccess: inv });
}

export function useStartRun(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async (input: Record<string, unknown>) => mapRun(await api.post<unknown>(`${base(org)}/workflows/${id}/runs`, { input })),
    onSuccess: inv,
  });
}

export interface TriggerCreated {
  id: string;
  secret: string | null;
  url: string | null;
  signature_header: string | null;
  timestamp_header: string | null;
  algorithm: string | null;
  format: string | null;
}

export function useCreateTrigger(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async (body: { type: "schedule" | "webhook" | "event"; config: Record<string, unknown> }): Promise<TriggerCreated> => {
      return triggerCreated((await api.post<Record<string, unknown>>(`${base(org)}/workflows/${id}/triggers`, body)) ?? {});
    },
    onSuccess: inv,
  });
}

function triggerCreated(r: Record<string, unknown>): TriggerCreated {
  const t = mapTrigger(r.trigger ?? r);
  const s = (k: string) => (typeof r[k] === "string" ? (r[k] as string) : null);
  return {
    id: t.id,
    secret: s("secret") ?? s("signing_secret"),
    url: t.url,
    signature_header: t.signature?.signature_header ?? s("signature_header"),
    timestamp_header: t.signature?.timestamp_header ?? s("timestamp_header"),
    algorithm: t.signature?.algorithm ?? null,
    format: t.signature?.format ?? null,
  };
}

export function useRotateTriggerSecret(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async (triggerId: string) => triggerCreated((await api.post<Record<string, unknown>>(`${base(org)}/workflows/${id}/triggers/${triggerId}/rotate-secret`, {})) ?? {}),
    onSuccess: inv,
  });
}

export function useSetTriggerActive(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: ({ triggerId, is_active }: { triggerId: string; is_active: boolean }) => api.patch<unknown>(`${base(org)}/workflows/${id}/triggers/${triggerId}`, { is_active }),
    onSuccess: inv,
  });
}

export function useRunAgent(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async ({ agentId, goal }: { agentId: string; goal: string }) => mapRun(await api.post<unknown>(`${base(org)}/agents/${agentId}/runs`, { goal })),
    onSuccess: inv,
  });
}

export function useCancelRun(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (runId: string) => api.post<unknown>(`${base(org)}/workflow-runs/${runId}/cancel`, {}), onSuccess: inv });
}

export function useDeleteTrigger(org: string, id: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (triggerId: string) => api.delete(`${base(org)}/workflows/${id}/triggers/${triggerId}`), onSuccess: inv });
}

export function useDecideApproval(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { id: string; decision: "approve" | "reject"; comment?: string; password?: string }) =>
      api.post<unknown>(`${base(org)}/approvals/${body.id}/decision`, { decision: body.decision, comment: body.comment || undefined, password: body.password || undefined }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: autoKeys.all(org) });
      void qc.invalidateQueries({ queryKey: ["org", org, "ml"] });
    },
  });
}

export interface AgentInput {
  name: string;
  description?: string;
  model_tier: string;
  allowed_tools: string[];
  tool_scopes: Record<string, Record<string, string[]>>;
  max_steps: number;
  approval_required_for: string[];
}

export function useSaveAgent(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: async ({ id, ...payload }: AgentInput & { id?: string }) => {
      return mapAgent(id ? await api.patch<unknown>(`${base(org)}/agents/${id}`, payload) : await api.post<unknown>(`${base(org)}/agents`, payload));
    },
    onSuccess: inv,
  });
}

export function useDeleteAgent(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (id: string) => api.delete(`${base(org)}/agents/${id}`), onSuccess: inv });
}
