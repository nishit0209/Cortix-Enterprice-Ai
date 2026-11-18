"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrapList } from "@/lib/api";
import type {
  CreateDataSourceRequest,
  DataSource,
  IntrospectSchema,
  IntrospectTable,
  JobAccepted,
  LineageGraph,
  Paginated,
  Pipeline,
  PipelineDag,
  PipelineRun,
  PipelineSchedule,
  RunLogLine,
  SemanticModel,
  MetricDefinition,
} from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export const deKeys = {
  sources: (org: string) => ["org", org, "data-sources"] as const,
  pipelines: (org: string) => ["org", org, "pipelines"] as const,
  pipeline: (org: string, id: string) => ["org", org, "pipelines", id] as const,
  runs: (org: string, id: string) => ["org", org, "pipelines", id, "runs"] as const,
  run: (org: string, runId: string) => ["org", org, "pipeline-runs", runId] as const,
  runLogs: (org: string, runId: string) => ["org", org, "pipeline-runs", runId, "logs"] as const,
  lineage: (org: string, type: string, id: string, depth: number) => ["org", org, "lineage", type, id, depth] as const,
  semantic: (org: string, datasetId: string) => ["org", org, "semantic", datasetId] as const,
};

/** Idempotency-Key for job-starting POSTs (API_SPECIFICATION §1). */
export function idempotencyKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

/* ---------------- data sources ---------------- */

export function useDataSources(org: string) {
  return useQuery({
    queryKey: deKeys.sources(org),
    queryFn: async ({ signal }) => unwrapList(await api.get<DataSource[] | Paginated<DataSource>>(`${o(org)}/data-sources`, { signal })),
  });
}

export function useCreateDataSource(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateDataSourceRequest) => api.post<DataSource>(`${o(org)}/data-sources`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.sources(org) }),
  });
}

export function useDeleteDataSource(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/data-sources/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.sources(org) }),
  });
}

export function startSourceJob(org: string, id: string, action: "test" | "introspect") {
  return api.post<JobAccepted>(`${o(org)}/data-sources/${id}/${action}`, undefined, { headers: { "Idempotency-Key": idempotencyKey() } });
}

export function importTable(org: string, id: string, body: { table: string; name: string; classification?: string; attest?: boolean }) {
  return api.post<JobAccepted & { dataset?: { id: string } }>(`${o(org)}/data-sources/${id}/import`, body, {
    headers: { "Idempotency-Key": idempotencyKey() },
  });
}

/** Introspection job results come as `{schemas:[{name,tables}]}` or a flat `{tables:[{schema,name,columns}]}`. */
export function parseIntrospection(result: unknown): IntrospectSchema[] {
  const r = (result ?? {}) as { schemas?: unknown; tables?: unknown };
  const schemas = new Map<string, IntrospectTable[]>();
  const pushTable = (schema: string, t: Record<string, unknown>) => {
    const table: IntrospectTable = {
      schema,
      name: String(t.name ?? t.table ?? ""),
      row_estimate: typeof t.row_estimate === "number" ? t.row_estimate : typeof t.rows === "number" ? t.rows : null,
      columns: (Array.isArray(t.columns) ? t.columns : []).map((c: Record<string, unknown>) => ({
        name: String(c.name ?? ""),
        type: String(c.type ?? c.data_type ?? ""),
        nullable: typeof c.nullable === "boolean" ? c.nullable : undefined,
      })),
    };
    if (table.name) schemas.set(schema, [...(schemas.get(schema) ?? []), table]);
  };
  if (Array.isArray(r.schemas)) {
    for (const s of r.schemas as Array<Record<string, unknown>>) {
      const name = String(s.name ?? s.schema ?? "public");
      if (!schemas.has(name)) schemas.set(name, []);
      for (const t of (Array.isArray(s.tables) ? s.tables : []) as Array<Record<string, unknown>>) pushTable(name, t);
    }
  }
  if (Array.isArray(r.tables)) {
    for (const t of r.tables as Array<Record<string, unknown>>) pushTable(String(t.schema ?? "public"), t);
  }
  return [...schemas.entries()].map(([name, tables]) => ({ name, tables: tables.sort((a, b) => a.name.localeCompare(b.name)) }));
}

/* ---------------- pipelines ---------------- */

export function usePipelines(org: string) {
  return useQuery({
    queryKey: deKeys.pipelines(org),
    queryFn: async ({ signal }) => unwrapList(await api.get<Pipeline[] | Paginated<Pipeline>>(`${o(org)}/pipelines`, { signal })),
    refetchInterval: (q) => ((q.state.data ?? []).some((p) => ["running", "queued", "retrying"].includes(p.last_run?.status ?? "")) ? 5_000 : false),
  });
}

export function usePipeline(org: string, id: string) {
  return useQuery({
    queryKey: deKeys.pipeline(org, id),
    queryFn: ({ signal }) => api.get<Pipeline>(`${o(org)}/pipelines/${id}`, { signal }),
  });
}

export function useCreatePipeline(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description?: string; dag: PipelineDag }) => api.post<Pipeline>(`${o(org)}/pipelines`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.pipelines(org) }),
  });
}

export function useUpdatePipeline(org: string, id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name?: string; description?: string; dag?: PipelineDag; status?: string }) => api.patch<Pipeline>(`${o(org)}/pipelines/${id}`, body),
    onSuccess: (p) => {
      qc.setQueryData(deKeys.pipeline(org, id), p);
      void qc.invalidateQueries({ queryKey: deKeys.pipelines(org) });
    },
  });
}

export function useDeletePipeline(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/pipelines/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.pipelines(org) }),
  });
}

export function runPipeline(org: string, id: string) {
  return api.post<JobAccepted & { run_id?: string; run?: PipelineRun }>(`${o(org)}/pipelines/${id}/runs`, undefined, {
    headers: { "Idempotency-Key": idempotencyKey() },
  });
}

/** Dry run / preview on a sample. Sync `{columns, rows, errors?}` or 202 job whose result carries the preview. */
export function previewPipeline(org: string, id: string, body: { dag: PipelineDag; node_id?: string; limit?: number }) {
  return api.post<
    | ({ columns: Array<{ name: string; type?: string } | string>; rows: unknown[][]; row_count?: number; errors?: Array<{ field: string | null; message: string }> } & { job_id?: undefined })
    | JobAccepted
  >(`${o(org)}/pipelines/${id}/validate`, body);
}

export function useSaveSchedule(org: string, id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (schedule: PipelineSchedule | null): Promise<PipelineSchedule | null> => {
      if (schedule) return api.put<PipelineSchedule>(`${o(org)}/pipelines/${id}/schedule`, schedule);
      await api.delete(`${o(org)}/pipelines/${id}/schedule`);
      return null;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: deKeys.pipeline(org, id) });
      void qc.invalidateQueries({ queryKey: deKeys.pipelines(org) });
    },
  });
}

export function usePipelineRuns(org: string, id: string, enabled = true) {
  return useQuery({
    queryKey: deKeys.runs(org, id),
    enabled,
    queryFn: async ({ signal }) => unwrapList(await api.get<PipelineRun[] | Paginated<PipelineRun>>(`${o(org)}/pipelines/${id}/runs`, { signal, query: { limit: 100 } })),
    refetchInterval: (q) => ((q.state.data ?? []).some((r) => ["running", "queued", "retrying"].includes(r.status)) ? 4_000 : false),
  });
}

export function usePipelineRun(org: string, runId: string) {
  return useQuery({
    queryKey: deKeys.run(org, runId),
    queryFn: ({ signal }) => api.get<PipelineRun>(`${o(org)}/pipeline-runs/${runId}`, { signal }),
    refetchInterval: (q) => (["running", "queued", "retrying"].includes(q.state.data?.status ?? "") ? 3_000 : false),
  });
}

export function useRunLogs(org: string, runId: string, live: boolean) {
  return useQuery({
    queryKey: deKeys.runLogs(org, runId),
    retry: false,
    queryFn: async ({ signal }) => unwrapList(await api.get<RunLogLine[] | Paginated<RunLogLine>>(`${o(org)}/pipeline-runs/${runId}/logs`, { signal, query: { limit: 500 } })),
    refetchInterval: live ? 3_000 : false,
  });
}

export function useRetryRun(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.post<JobAccepted & { run_id?: string }>(`${o(org)}/pipeline-runs/${runId}/retry`, undefined, { headers: { "Idempotency-Key": idempotencyKey() } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["org", org, "pipelines"] }),
  });
}

/* ---------------- lineage ---------------- */

export function useLineage(org: string, nodeType: string, nodeId: string | null, depth = 3) {
  return useQuery({
    queryKey: deKeys.lineage(org, nodeType, nodeId ?? "", depth),
    enabled: !!nodeId,
    retry: false,
    queryFn: ({ signal }) => api.get<LineageGraph>(`${o(org)}/lineage`, { signal, query: { node_type: nodeType, node_id: nodeId, depth } }),
  });
}

/* ---------------- semantic layer ---------------- */

/** The semantic model of a dataset (null when none exists yet). GET /semantic-models?dataset_id= then the detail. */
export function useSemanticModel(org: string, datasetId: string, enabled = true) {
  return useQuery({
    queryKey: deKeys.semantic(org, datasetId),
    enabled,
    retry: false,
    queryFn: async ({ signal }) => {
      const list = unwrapList(
        await api.get<SemanticModel[] | Paginated<SemanticModel>>(`${o(org)}/semantic-models`, { signal, query: { dataset_id: datasetId } }),
      ).filter((m) => m.dataset_id === datasetId);
      if (list.length === 0) return null;
      const latest = [...list].sort((a, b) => (b.version ?? 0) - (a.version ?? 0))[0];
      const full = latest.tables ? latest : await api.get<SemanticModel>(`${o(org)}/semantic-models/${latest.id}`, { signal });
      return { ...full, tables: full.tables ?? [], metrics: full.metrics ?? [] };
    },
  });
}

export function useCreateSemanticModel(org: string, datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.post<SemanticModel>(`${o(org)}/semantic-models`, { dataset_id: datasetId, name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.semantic(org, datasetId) }),
  });
}

export function useUpdateSemanticModel(org: string, datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, tables }: { id: string; tables: SemanticModel["tables"] }) => api.patch<SemanticModel>(`${o(org)}/semantic-models/${id}`, { tables }),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.semantic(org, datasetId) }),
  });
}

export function usePublishSemanticModel(org: string, datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<SemanticModel>(`${o(org)}/semantic-models/${id}/publish`),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.semantic(org, datasetId) }),
  });
}

export function useSaveMetric(org: string, datasetId: string, modelId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (m: MetricDefinition) =>
      m.id
        ? api.patch<MetricDefinition>(`${o(org)}/semantic-models/${modelId}/metrics/${m.id}`, m)
        : api.post<MetricDefinition>(`${o(org)}/semantic-models/${modelId}/metrics`, m),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.semantic(org, datasetId) }),
  });
}

export function useDeleteMetric(org: string, datasetId: string, modelId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/semantic-models/${modelId}/metrics/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.semantic(org, datasetId) }),
  });
}
