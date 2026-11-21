"use client";

/** Platform super-admin API (`/api/v1/platform/*`, `apps/platform_admin`). Counts and status only, never tenant rows. */

import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, cursorFrom } from "@/lib/api";
import type { QuotaBody } from "@/lib/platform";
import type { Paginated } from "@/lib/types";

export interface PlatformOrg {
  id: string;
  name: string;
  slug: string;
  status: "active" | "suspended" | "deleted" | string;
  plan: string;
  region: string;
  created_at: string;
  members_count: number;
  datasets_count: number;
  storage_bytes: number;
  ai_usage: { window_days: number; requests: number; tokens: number };
}

export interface PlatformOrgDetail extends PlatformOrg {
  suspended_reason: string | null;
  members_by_role: Record<string, number>;
  datasets_by_status: Record<string, number>;
  documents_by_status: Record<string, number>;
  pipelines_by_status: Record<string, number>;
  jobs_by_status: Record<string, number>;
  ai_daily: Array<{ day: string; requests: number; tokens: number }>;
  last_activity_at: string | null;
  platform_actions: Array<{ occurred_at: string; action: string; actor_email: string | null; reason: string | null }>;
}

export interface HealthComponent {
  status: string;
  latency_ms?: number;
  error?: string;
  detail?: string;
  [k: string]: unknown;
}

export interface PlatformHealth {
  status: string;
  components: Record<string, HealthComponent>;
  runtime: Record<string, unknown>;
  checked_at: string;
}

export interface PlatformJobs {
  runner: Record<string, unknown>;
  by_status: Record<string, number>;
  queue: { by_type: Record<string, number>; depth: number; oldest_queued_at: string | null };
  failures_24h: number;
  items: Array<{ id: string; org_id: string; org_slug: string | null; type: string; status: string; created_at: string; finished_at: string | null; error_code: string | null }>;
}

export interface ModelQuota {
  model: string;
  configured: boolean;
  rpm: number | null;
  tpm: number | null;
  rpd: number | null;
  verified_at: string | null;
  notes: string;
  updated_at: string | null;
  effective: { rpm: number; tpm: number; rpd: number } | null;
}

export interface PlatformAiUsage {
  range: { from: string; days: number };
  totals: { requests: number; input_tokens: number; output_tokens: number; blocked: number; errors: number };
  daily: Array<{ day: string; model: string; requests: number; input_tokens: number; output_tokens: number; blocked: number; errors: number }>;
  top_orgs: Array<{ org_id: string; org_slug: string | null; requests: number }>;
  models: ModelQuota[];
  configured: boolean;
  tier: string;
}

export interface PlatformLogs {
  items: Array<{ id: number; created_at: string; level: string; component: string; message: string; where: string; exc_type: string | null; request_id: string | null }>;
  next_before_id: number | null;
  capture: { enabled: boolean; max_rows: number; dropped: number };
}

const P = "/platform";
export const platformKeys = {
  all: ["platform"] as const,
  orgs: (f: Record<string, string>) => ["platform", "orgs", f] as const,
  org: (id: string) => ["platform", "org", id] as const,
  health: ["platform", "health"] as const,
  jobs: (f: Record<string, string>) => ["platform", "jobs", f] as const,
  ai: (days: number) => ["platform", "ai", days] as const,
  logs: (f: Record<string, string>) => ["platform", "logs", f] as const,
};

const clean = (f: Record<string, string>) => Object.fromEntries(Object.entries(f).filter(([, v]) => !!v));

export function usePlatformOrgs(filters: { status: string; search: string }, enabled = true) {
  const f = clean(filters);
  return useInfiniteQuery({
    queryKey: platformKeys.orgs(f),
    enabled,
    retry: false,
    initialPageParam: null as string | null,
    placeholderData: keepPreviousData,
    queryFn: ({ pageParam, signal }) => api.get<Paginated<PlatformOrg>>(`${P}/orgs`, { signal, query: { ...f, limit: 50, cursor: pageParam ?? undefined } }),
    getNextPageParam: (last) => cursorFrom(last.next_cursor),
  });
}

export function usePlatformOrg(id: string | null) {
  return useQuery({
    queryKey: platformKeys.org(id ?? ""),
    enabled: !!id,
    retry: false,
    queryFn: ({ signal }) => api.get<PlatformOrgDetail>(`${P}/orgs/${id}`, { signal }),
  });
}

export function useOrgStatusMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action, reason }: { id: string; action: "suspend" | "reactivate"; reason: string }) =>
      api.post<PlatformOrgDetail>(`${P}/orgs/${id}/${action}`, { reason }),
    onSuccess: (data) => {
      qc.setQueryData(platformKeys.org(data.id), data);
      void qc.invalidateQueries({ queryKey: ["platform", "orgs"] });
    },
  });
}

export function usePlatformHealth(enabled = true) {
  return useQuery({
    queryKey: platformKeys.health,
    enabled,
    retry: false,
    refetchInterval: 30_000,
    queryFn: ({ signal }) => api.get<PlatformHealth>(`${P}/health`, { signal }),
  });
}

export function usePlatformJobs(filters: { queue: string; status: string }, enabled = true) {
  const f = clean(filters);
  return useQuery({
    queryKey: platformKeys.jobs(f),
    enabled,
    retry: false,
    refetchInterval: 15_000,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => api.get<PlatformJobs>(`${P}/jobs`, { signal, query: { ...f, limit: 100 } }),
  });
}

export function usePlatformAiUsage(days: number, enabled = true) {
  return useQuery({
    queryKey: platformKeys.ai(days),
    enabled,
    retry: false,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => api.get<PlatformAiUsage>(`${P}/ai/usage`, { signal, query: { days } }),
  });
}

export function useSetModelQuota() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ model, body }: { model: string; body: QuotaBody }) => api.put<ModelQuota>(`${P}/ai/model-quotas/${encodeURIComponent(model)}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["platform", "ai"] }),
  });
}

export function usePlatformLogs(filters: { level: string; component: string }, enabled = true) {
  const f = clean(filters);
  return useInfiniteQuery({
    queryKey: platformKeys.logs(f),
    enabled,
    retry: false,
    initialPageParam: null as number | null,
    placeholderData: keepPreviousData,
    queryFn: ({ pageParam, signal }) => api.get<PlatformLogs>(`${P}/logs`, { signal, query: { ...f, limit: 100, before_id: pageParam ?? undefined } }),
    getNextPageParam: (last) => last.next_before_id,
  });
}
