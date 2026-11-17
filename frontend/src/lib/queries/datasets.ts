"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrapList, uploadWithProgress, type UploadProgress } from "@/lib/api";
import type {
  Classification,
  CreateDatasetRequest,
  CreateDatasetResponse,
  Dataset,
  DatasetColumn,
  DatasetPreview,
  Job,
  Paginated,
  QualityReport,
  Upload,
} from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export const datasetKeys = {
  list: (org: string) => ["org", org, "datasets"] as const,
  detail: (org: string, id: string) => ["org", org, "datasets", id] as const,
  columns: (org: string, id: string) => ["org", org, "datasets", id, "columns"] as const,
  preview: (org: string, id: string) => ["org", org, "datasets", id, "preview"] as const,
  quality: (org: string, id: string) => ["org", org, "datasets", id, "quality"] as const,
  job: (org: string, id: string) => ["org", org, "jobs", id] as const,
};

const ACTIVE = new Set(["pending", "queued", "ingesting", "profiling", "processing", "running"]);
export const isDatasetProcessing = (d: Pick<Dataset, "status"> | undefined) => !!d && ACTIVE.has(String(d.status));

export function useDatasets(org: string) {
  return useQuery({
    queryKey: datasetKeys.list(org),
    queryFn: async ({ signal }) =>
      unwrapList(await api.get<Dataset[] | Paginated<Dataset>>(`${o(org)}/datasets`, { signal, query: { limit: 200 } })),
    // Keep the list fresh while any dataset is still being processed.
    refetchInterval: (q) => ((q.state.data ?? []).some((d) => isDatasetProcessing(d)) ? 5_000 : false),
  });
}

export function useDataset(org: string, id: string) {
  return useQuery({
    queryKey: datasetKeys.detail(org, id),
    queryFn: ({ signal }) => api.get<Dataset>(`${o(org)}/datasets/${id}`, { signal }),
    refetchInterval: (q) => (isDatasetProcessing(q.state.data) ? 4_000 : false),
  });
}

export function useDatasetColumns(org: string, id: string, enabled = true) {
  return useQuery({
    queryKey: datasetKeys.columns(org, id),
    enabled,
    queryFn: async ({ signal }) =>
      unwrapList(await api.get<DatasetColumn[] | Paginated<DatasetColumn>>(`${o(org)}/datasets/${id}/columns`, { signal })),
  });
}

export function useDatasetPreview(org: string, id: string, enabled = true, limit = 100) {
  return useQuery({
    queryKey: [...datasetKeys.preview(org, id), limit],
    enabled,
    queryFn: ({ signal }) => api.get<DatasetPreview>(`${o(org)}/datasets/${id}/preview`, { signal, query: { limit } }),
  });
}

export function useDatasetQuality(org: string, id: string, enabled = true) {
  return useQuery({
    queryKey: datasetKeys.quality(org, id),
    enabled,
    queryFn: ({ signal }) => api.get<QualityReport>(`${o(org)}/datasets/${id}/quality`, { signal }),
  });
}

export function useJob(org: string, id: string | null) {
  return useQuery({
    queryKey: datasetKeys.job(org, id ?? ""),
    enabled: !!id,
    queryFn: ({ signal }) => api.get<Job>(`${o(org)}/jobs/${id}`, { signal }),
  });
}

export function uploadFile(org: string, file: File, onProgress?: (p: UploadProgress) => void, signal?: AbortSignal) {
  const form = new FormData();
  form.append("file", file);
  return uploadWithProgress<Upload>(`${o(org)}/uploads`, form, onProgress, signal);
}

export function useCreateDataset(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateDatasetRequest) => api.post<CreateDatasetResponse>(`${o(org)}/datasets`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: datasetKeys.list(org) });
      void qc.invalidateQueries({ queryKey: ["org", org, "data-stats"] });
    },
  });
}

export function useClassifyDataset(org: string, id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (classification: Classification) =>
      api.post<Dataset>(`${o(org)}/datasets/${id}/classification`, { classification, attest: true }),
    onSuccess: (data) => {
      qc.setQueryData(datasetKeys.detail(org, id), data);
      void qc.invalidateQueries({ queryKey: datasetKeys.list(org) });
    },
  });
}

export function useDeleteDataset(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/datasets/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: datasetKeys.list(org) });
      void qc.invalidateQueries({ queryKey: ["org", org, "data-stats"] });
    },
  });
}
