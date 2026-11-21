"use client";

/**
 * ML Workspace queries (API_SPECIFICATION Phase 9 + CONTRACT "W4 additions"). Raw payloads are normalized by
 * `lib/ml/mappers.ts`. Endpoints that the parallel backend may not have yet surface as 404/405/501, which the
 * screens show as "not available yet" (`isUnavailableEndpoint`).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isApiError } from "@/lib/api";
import { isUnavailableEndpoint } from "@/lib/errors";
import {
  listOf,
  mapEda,
  mapExperiment,
  mapModel,
  mapMonitoring,
  mapPredictResult,
  mapProject,
  mapRecommendations,
  isObj,
  num,
} from "@/lib/ml/mappers";
import type { CreateProjectRequest, Experiment, FeaturePreview, ModelStage, TaskType } from "@/lib/ml/types";
import { idempotencyKey } from "./pipelines";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}/ml`;
const JOB = () => ({ headers: { "Idempotency-Key": idempotencyKey() } });

export const mlKeys = {
  all: (org: string) => ["org", org, "ml"] as const,
  projects: (org: string) => ["org", org, "ml", "projects"] as const,
  project: (org: string, id: string) => ["org", org, "ml", "projects", id] as const,
  eda: (org: string, id: string) => ["org", org, "ml", "projects", id, "eda"] as const,
  recommendations: (org: string, id: string) => ["org", org, "ml", "projects", id, "recommendations"] as const,
  experiments: (org: string, projectId: string) => ["org", org, "ml", "projects", projectId, "experiments"] as const,
  experiment: (org: string, id: string) => ["org", org, "ml", "experiments", id] as const,
  compare: (org: string, ids: string[]) => ["org", org, "ml", "compare", [...ids].sort().join(",")] as const,
  models: (org: string) => ["org", org, "ml", "models"] as const,
  model: (org: string, id: string) => ["org", org, "ml", "models", id] as const,
  monitoring: (org: string, id: string) => ["org", org, "ml", "models", id, "monitoring"] as const,
  preview: (org: string, datasetId: string, target: string | null, task: string | null, time: string | null) =>
    ["org", org, "ml", "preview", datasetId, target, task, time] as const,
};

/** Job-accepted bodies carry the job id plus (usually) the created resource id. */
export interface MlJobAccepted {
  job_id: string | null;
  id: string | null;
}

function jobAccepted(raw: unknown, ...idKeys: string[]): MlJobAccepted {
  const r = isObj(raw) ? raw : {};
  let id: string | null = null;
  for (const k of idKeys) {
    const v = r[k];
    if (typeof v === "string") id = v;
    else if (isObj(v) && typeof v.id === "string") id = v.id;
    if (id) break;
  }
  return { job_id: typeof r.job_id === "string" ? r.job_id : null, id };
}

const ACTIVE = new Set(["queued", "running", "pending"]);

/* ---------------- projects ---------------- */

export function useMlProjects(org: string) {
  return useQuery({
    queryKey: mlKeys.projects(org),
    retry: (n, e) => !isUnavailableEndpoint(e) && n < 1,
    queryFn: async ({ signal }) => listOf(await api.get<unknown>(`${o(org)}/projects`, { signal, query: { limit: 200 } }), "projects").map(mapProject),
  });
}

export function useMlProject(org: string, id: string) {
  return useQuery({
    queryKey: mlKeys.project(org, id),
    retry: (n, e) => !isUnavailableEndpoint(e) && n < 1,
    queryFn: async ({ signal }) => mapProject(await api.get<unknown>(`${o(org)}/projects/${id}`, { signal })),
  });
}

export function useCreateMlProject(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateProjectRequest) => mapProject(await api.post<unknown>(`${o(org)}/projects`, body)),
    onSuccess: (p) => {
      qc.setQueryData(mlKeys.project(org, p.id), p);
      void qc.invalidateQueries({ queryKey: mlKeys.projects(org), exact: true });
    },
  });
}

export function useDeleteMlProject(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/projects/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: mlKeys.projects(org), exact: true }),
  });
}

/**
 * Server feature preview for the wizard: `POST /ml/projects/preview {dataset_id, target_column, task_type,
 * time_column}` -> `{detected_task_type, features|candidates, excluded_by_policy, leakage_dropped}`. Resolves to
 * `null` when the route does not exist (the wizard then computes a local preview and says so).
 */
export function useProjectPreview(
  org: string,
  args: { datasetId: string | null; target: string | null; task: TaskType | null; timeColumn: string | null; enabled: boolean },
) {
  return useQuery({
    queryKey: mlKeys.preview(org, args.datasetId ?? "", args.target, args.task, args.timeColumn),
    enabled: args.enabled && !!args.datasetId && !!args.task,
    retry: false,
    staleTime: 60_000,
    queryFn: async ({ signal }): Promise<FeaturePreview | null> => {
      try {
        const raw = await api.post<unknown>(
          `${o(org)}/projects/preview`,
          { dataset_id: args.datasetId, target_column: args.target, task_type: args.task, time_column: args.timeColumn },
          { signal },
        );
        const p = mapProject({ ...(isObj(raw) ? raw : {}), id: "preview" });
        const r = isObj(raw) ? raw : {};
        const candidates = listOf(r.candidates ?? r.features).map((f) => (isObj(f) ? String(f.name ?? "") : String(f))).filter(Boolean);
        return {
          detected_task_type: (typeof r.detected_task_type === "string" ? (r.detected_task_type as TaskType) : null) ?? null,
          candidates,
          excluded_by_policy: p.excluded_by_policy,
          leakage_dropped: p.leakage_dropped,
          server: true,
        };
      } catch (e) {
        if (isUnavailableEndpoint(e)) return null;
        throw e;
      }
    },
  });
}

/* ---------------- EDA / recommendations ---------------- */

/** Latest EDA: `GET /projects/{id}/eda` (404 = not run yet), falling back to the project's embedded `eda`. */
export function useMlEda(org: string, id: string) {
  return useQuery({
    queryKey: mlKeys.eda(org, id),
    retry: false,
    queryFn: async ({ signal }) => {
      try {
        return mapEda(await api.get<unknown>(`${o(org)}/projects/${id}/eda`, { signal }));
      } catch (e) {
        if (isApiError(e) && e.status === 404) return null;
        throw e;
      }
    },
  });
}

export function useRunEda(org: string, id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => jobAccepted(await api.post<unknown>(`${o(org)}/projects/${id}/eda`, {}, JOB()), "eda_id", "snapshot_id", "snapshot"),
    onSuccess: () => qc.invalidateQueries({ queryKey: mlKeys.eda(org, id), exact: true }),
  });
}

export function useMlRecommendations(org: string, id: string, enabled = true) {
  return useQuery({
    queryKey: mlKeys.recommendations(org, id),
    enabled,
    retry: false,
    queryFn: async ({ signal }) => {
      const raw = await api.get<unknown>(`${o(org)}/projects/${id}/recommendations`, { signal });
      const r = isObj(raw) ? raw : {};
      return {
        items: mapRecommendations(raw),
        primary_metric: typeof r.primary_metric === "string" ? r.primary_metric : null,
        notes: listOf(r.notes ?? r.warnings).map(String),
      };
    },
  });
}

/* ---------------- experiments ---------------- */

export function useMlExperiments(org: string, projectId: string) {
  return useQuery({
    queryKey: mlKeys.experiments(org, projectId),
    retry: (n, e) => !isUnavailableEndpoint(e) && n < 1,
    queryFn: async ({ signal }) =>
      listOf(await api.get<unknown>(`${o(org)}/projects/${projectId}/experiments`, { signal, query: { limit: 200 } }), "experiments").map(mapExperiment),
    refetchInterval: (q) => ((q.state.data ?? []).some((e) => ACTIVE.has(String(e.status))) ? 5_000 : false),
  });
}

export function useMlExperiment(org: string, id: string | null) {
  return useQuery({
    queryKey: mlKeys.experiment(org, id ?? ""),
    enabled: !!id,
    queryFn: async ({ signal }) => mapExperiment(await api.get<unknown>(`${o(org)}/experiments/${id}`, { signal })),
    refetchInterval: (q) => (q.state.data && ACTIVE.has(String(q.state.data.status)) ? 4_000 : false),
  });
}

/**
 * Side-by-side comparison: `GET /experiments/compare?ids=` (`{experiments|items, best_id?, primary_metric?}`).
 * When the compare route is unavailable, each experiment detail is fetched instead.
 */
export function useCompareExperiments(org: string, ids: string[]) {
  const sorted = [...ids].sort();
  return useQuery({
    queryKey: mlKeys.compare(org, sorted),
    enabled: ids.length >= 2,
    retry: false,
    queryFn: async ({ signal }): Promise<{ experiments: Experiment[]; best_id: string | null; primary_metric: string | null }> => {
      try {
        const raw = await api.get<unknown>(`${o(org)}/experiments/compare`, { signal, query: { ids: sorted.join(",") } });
        const r = isObj(raw) ? raw : {};
        const experiments = listOf(raw, "experiments").map(mapExperiment);
        return {
          experiments: ids.map((id) => experiments.find((e) => e.id === id)).filter((e): e is Experiment => !!e),
          best_id: typeof r.best_id === "string" ? r.best_id : null,
          primary_metric: typeof r.primary_metric === "string" ? r.primary_metric : null,
        };
      } catch (e) {
        if (!isUnavailableEndpoint(e)) throw e;
        const experiments = await Promise.all(ids.map(async (id) => mapExperiment(await api.get<unknown>(`${o(org)}/experiments/${id}`, { signal }))));
        return { experiments, best_id: null, primary_metric: null };
      }
    },
  });
}

export interface StartExperimentRequest {
  algorithm: string;
  hyperparameters: Record<string, unknown>;
  random_state: number;
  test_size?: number;
  cv_folds?: number;
}

export function useStartExperiment(org: string, projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: StartExperimentRequest) =>
      jobAccepted(await api.post<unknown>(`${o(org)}/projects/${projectId}/experiments`, body, JOB()), "experiment_id", "experiment", "id"),
    onSuccess: () => qc.invalidateQueries({ queryKey: mlKeys.experiments(org, projectId), exact: true }),
  });
}

export function useRegisterExperiment(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, name }: { id: string; name?: string }) => {
      const raw = await api.post<unknown>(`${o(org)}/experiments/${id}/register`, name ? { name } : {});
      const r = isObj(raw) ? raw : {};
      const model = isObj(r.model) ? r.model : null;
      return { model_id: (typeof r.model_id === "string" ? r.model_id : model && typeof model.id === "string" ? model.id : null) as string | null, version: num(r.version) ?? (isObj(r.version) ? num(r.version.version) : null) };
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: mlKeys.models(org) }),
  });
}

/* ---------------- registry ---------------- */

export function useMlModels(org: string) {
  return useQuery({
    queryKey: mlKeys.models(org),
    retry: (n, e) => !isUnavailableEndpoint(e) && n < 1,
    queryFn: async ({ signal }) => listOf(await api.get<unknown>(`${o(org)}/models`, { signal, query: { limit: 200 } }), "models").map(mapModel),
  });
}

/** Model detail with its versions (`GET /models/{id}`; versions inline or at `/models/{id}/versions`). */
export function useMlModel(org: string, id: string) {
  return useQuery({
    queryKey: mlKeys.model(org, id),
    retry: (n, e) => !isUnavailableEndpoint(e) && n < 1,
    queryFn: async ({ signal }) => {
      const model = mapModel(await api.get<unknown>(`${o(org)}/models/${id}`, { signal }));
      if (model.versions.length) return model;
      try {
        const versions = mapModel({ versions: await api.get<unknown>(`${o(org)}/models/${id}/versions`, { signal }) }).versions;
        return { ...model, versions, latest_version: model.latest_version ?? versions[0]?.version ?? null };
      } catch (e) {
        if (isUnavailableEndpoint(e)) return model;
        throw e;
      }
    },
  });
}

export function usePromoteVersion(org: string, modelId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ version, stage }: { version: number; stage: Exclude<ModelStage, "none"> }) =>
      api.post<unknown>(`${o(org)}/models/${modelId}/versions/${version}/promote`, { stage }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: mlKeys.model(org, modelId), exact: true });
      void qc.invalidateQueries({ queryKey: mlKeys.models(org), exact: true });
    },
  });
}

export function usePredict(org: string, modelId: string) {
  return useMutation({
    mutationFn: async ({ rows, version, explain = true }: { rows: Array<Record<string, unknown>>; version?: number | null; explain?: boolean }) =>
      mapPredictResult(await api.post<unknown>(`${o(org)}/models/${modelId}/predict`, { rows, explain, ...(version ? { version } : {}) })),
  });
}

export function useBatchPredict(org: string, modelId: string) {
  return useMutation({
    mutationFn: async (body: { dataset_id: string; output_name?: string; version?: number | null }) =>
      jobAccepted(await api.post<unknown>(`${o(org)}/models/${modelId}/batch-predictions`, body, JOB()), "output_dataset_id", "batch_id", "id"),
  });
}

export function useMlMonitoring(org: string, modelId: string, enabled = true) {
  return useQuery({
    queryKey: mlKeys.monitoring(org, modelId),
    enabled,
    retry: false,
    queryFn: async ({ signal }) => mapMonitoring(await api.get<unknown>(`${o(org)}/models/${modelId}/monitoring`, { signal })),
  });
}
