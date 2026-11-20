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
  PipelinePreview,
  PipelineSchedule,
  PipelineValidation,
  RunLogLine,
  SourceCheck,
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

/**
 * Idempotency-Key for job-starting POSTs (API_SPECIFICATION §1). `randomUUID` needs a secure context (plain
 * http on a LAN address is not one); `getRandomValues` works everywhere.
 */
export function idempotencyKey(): string {
  const c: Crypto = globalThis.crypto;
  if (typeof c.randomUUID === "function") return c.randomUUID();
  return Array.from(c.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
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

export interface ImportTableRequest {
  table: string;
  schema?: string;
  name: string;
  columns?: string[];
  classification?: string;
  attest?: boolean;
  mode?: "full" | "incremental";
  cursor_column?: string;
  dataset_id?: string;
}

export function importTable(org: string, id: string, body: ImportTableRequest) {
  return api.post<JobAccepted & { import?: { id: string; dataset_id?: string | null } }>(`${o(org)}/data-sources/${id}/import`, body, {
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
      kind: typeof t.kind === "string" ? t.kind : undefined,
      row_estimate:
        typeof t.estimated_rows === "number"
          ? t.estimated_rows
          : typeof t.row_estimate === "number"
            ? t.row_estimate
            : typeof t.rows === "number"
              ? t.rows
              : null,
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

/**
 * Connection-test checks for the UI. The backend test result is `{ok, server_version, current_user, database,
 * resolved_ip, latency_ms, readonly: {ok, problems[]}, code, message}`; older payloads may already carry `checks`.
 */
export function testChecks(result: unknown): SourceCheck[] | null {
  if (!result || typeof result !== "object") return null;
  const r = result as Record<string, unknown>;
  if (Array.isArray(r.checks)) return r.checks as SourceCheck[];
  if (!("ok" in r) && !("readonly" in r)) return null;
  const ro = (r.readonly ?? {}) as { ok?: boolean; problems?: string[] };
  const connected = !!r.server_version || (r.ok === true);
  const out: SourceCheck[] = [
    {
      key: "login",
      ok: connected,
      message: connected
        ? [r.server_version ? `PostgreSQL ${String(r.server_version)}` : null, r.current_user ? `as ${String(r.current_user)}` : null, r.resolved_ip ? `at ${String(r.resolved_ip)}` : null, typeof r.latency_ms === "number" ? `${r.latency_ms} ms` : null]
            .filter(Boolean)
            .join(" · ")
        : String(r.message || r.code || "Could not connect."),
    },
  ];
  if (connected) {
    out.push({ key: "readonly", ok: ro.ok === true, message: ro.ok ? "The role cannot write." : (ro.problems ?? []).join(", ") || "The role can write." });
  }
  return out;
}

/** GET /data-sources/{id}/introspect: the last stored catalog (no job is started). */
export function useSourceCatalog(org: string, id: string, enabled = true) {
  return useQuery({
    queryKey: [...deKeys.sources(org), id, "catalog"] as const,
    enabled,
    retry: false,
    queryFn: async ({ signal }) => {
      const res = await api.get<{ introspected_at: string | null; catalog: unknown }>(`${o(org)}/data-sources/${id}/introspect`, { signal });
      return { introspectedAt: res.introspected_at, schemas: parseIntrospection(res.catalog) };
    },
  });
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

/** POST /pipelines/{id}/preview: synchronous dry run of the (unsaved) DAG on a sample. To preview one step the
 * builder sends the DAG cut after that step (`dagUpTo`) plus `node_id` (see the W3 handoff defect on `node_id`). */
export function previewPipeline(org: string, id: string, body: { dag: PipelineDag; node_id?: string; limit?: number }) {
  return api.post<PipelinePreview>(`${o(org)}/pipelines/${id}/preview`, body);
}

/** POST /pipelines/validate: server-side validation with per-node issues and inferred schemas. */
export function validatePipeline(org: string, dag: PipelineDag) {
  return api.post<PipelineValidation>(`${o(org)}/pipelines/validate`, { dag });
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

/* ---------------- semantic layer (`apps/semantic`, dataset scoped) ---------------- */

/** GET /datasets/{id}/semantic-model. The backend generates the model on first access. */
export function useSemanticModel(org: string, datasetId: string, enabled = true) {
  return useQuery({
    queryKey: deKeys.semantic(org, datasetId),
    enabled,
    retry: false,
    queryFn: async ({ signal }) => normalizeModel(await api.get<SemanticModel>(`${o(org)}/datasets/${datasetId}/semantic-model`, { signal })),
  });
}

function normalizeModel(m: SemanticModel): SemanticModel {
  return { ...m, tables: m.tables ?? [], metrics: m.metrics ?? [] };
}

export interface SemanticPatch {
  description?: string;
  tables?: Array<{ name: string; display_name?: string; description?: string; synonyms?: string[] }>;
  columns?: Array<{
    name: string;
    table?: string;
    display_name?: string;
    description?: string;
    synonyms?: string[];
    role?: string;
    default_agg?: string | null;
    exposed_to_ai?: boolean;
  }>;
}

const COLUMN_FIELDS = ["display_name", "description", "synonyms", "role", "default_agg", "exposed_to_ai"] as const;

/** Minimal PATCH body: only the tables/columns (and fields) that differ from the saved model. */
export function semanticDiff(saved: Pick<SemanticModel, "tables">, edited: SemanticModel["tables"]): SemanticPatch {
  const patch: SemanticPatch = {};
  const tables: NonNullable<SemanticPatch["tables"]> = [];
  const columns: NonNullable<SemanticPatch["columns"]> = [];
  const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  for (const t of edited) {
    const before = saved.tables.find((x) => x.name === t.name);
    if (!before) continue;
    const tp: NonNullable<SemanticPatch["tables"]>[number] = { name: t.name };
    if (!same(t.display_name, before.display_name)) tp.display_name = t.display_name;
    if (!same(t.description, before.description)) tp.description = t.description;
    if (!same(t.synonyms, before.synonyms)) tp.synonyms = t.synonyms;
    if (Object.keys(tp).length > 1) tables.push(tp);
    for (const c of t.columns) {
      const bc = before.columns.find((x) => x.name === c.name);
      if (!bc) continue;
      const cp: Record<string, unknown> = { name: c.name, table: t.name };
      for (const k of COLUMN_FIELDS) if (!same(c[k], bc[k])) cp[k] = c[k] ?? null;
      if (Object.keys(cp).length > 2) columns.push(cp as NonNullable<SemanticPatch["columns"]>[number]);
    }
  }
  if (tables.length) patch.tables = tables;
  if (columns.length) patch.columns = columns;
  return patch;
}

export function useUpdateSemanticModel(org: string, datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SemanticPatch) => api.patch<SemanticModel>(`${o(org)}/datasets/${datasetId}/semantic-model`, body),
    onSuccess: (m) => qc.setQueryData(deKeys.semantic(org, datasetId), normalizeModel(m)),
  });
}

/** POST /semantic-model/refresh: regenerate from the current dataset version (`reset` also drops manual edits). */
export function useRefreshSemanticModel(org: string, datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (reset: boolean) => api.post<SemanticModel>(`${o(org)}/datasets/${datasetId}/semantic-model/refresh`, { reset }),
    onSuccess: (m) => qc.setQueryData(deKeys.semantic(org, datasetId), normalizeModel(m)),
  });
}

export function useSaveMetric(org: string, datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (m: MetricDefinition) => {
      const body = { name: m.name, expression: m.expression, description: m.description ?? "", synonyms: m.synonyms ?? [] };
      return m.id
        ? api.patch<MetricDefinition>(`${o(org)}/datasets/${datasetId}/metrics/${m.id}`, {
            ...body,
            ...(m.is_active !== undefined ? { is_active: m.is_active } : {}),
          })
        : api.post<MetricDefinition>(`${o(org)}/datasets/${datasetId}/metrics`, { ...body, ...(m.key ? { key: m.key } : {}) });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.semantic(org, datasetId) }),
  });
}

export function useDeleteMetric(org: string, datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/datasets/${datasetId}/metrics/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: deKeys.semantic(org, datasetId) }),
  });
}
