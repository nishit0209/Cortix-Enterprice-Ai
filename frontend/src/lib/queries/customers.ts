"use client";

/**
 * Customer 360 (API_SPECIFICATION Phase 6; as built in `apps/customers/views.py`). Every list is the cursor
 * envelope; jobs answer 202 with `{job_id, status_url, events_url}` (+ `run_id` for resolution).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fetchAll, type Query } from "@/lib/api";
import { isUnavailableEndpoint } from "@/lib/errors";
import type { SegmentDefinition } from "@/lib/mdm";
import { useCursorList } from "./paged";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}/customers`;
const retry = (n: number, e: unknown) => !isUnavailableEndpoint(e) && n < 1;

export type CustomerSourceKind = "profile" | "event";

export interface CustomerSource {
  id: string;
  name: string;
  source_system: string;
  kind: CustomerSourceKind;
  dataset_id: string;
  mapping: Record<string, string | string[]>;
  link_source_id: string | null;
  priority: number;
  status: string;
  last_error: string | null;
  record_count: number;
  last_synced_at: string | null;
  created_at: string;
}

export interface CustomerRun {
  id: string;
  status: string;
  job_id: string | null;
  stats: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface SourceRecord {
  id: string;
  source_system?: string;
  source_key?: string;
  attributes?: Record<string, unknown>;
  profile_id?: string | null;
  recorded_at?: string | null;
}

export interface IdentityMatch {
  id: string;
  score: number;
  status: string;
  features: Record<string, unknown>;
  record_a: SourceRecord;
  record_b: SourceRecord;
  reviewed_by: string | null;
  decided_at: string | null;
  note: string;
  created_at: string;
}

export interface CustomerScore {
  id: string;
  score_type: string;
  value: unknown;
  confidence: number | null;
  explanation: unknown;
  model_id: string | null;
  model_version_id: string | null;
  scored_at: string;
}

export interface CustomerProfile {
  id: string;
  display_name: string;
  email: string | null;
  phone: string | null;
  golden: Record<string, unknown>;
  record_count: number;
  sources: string[];
  event_count: number;
  order_count: number;
  monetary: number;
  first_seen: string | null;
  last_seen: string | null;
  last_order_at: string | null;
  created_at: string;
  updated_at: string;
  segment?: Record<string, unknown>;
}

export interface CustomerDetail extends CustomerProfile {
  survivorship: Record<string, unknown> | null;
  records: SourceRecord[];
  segments: Array<{ id: string; name: string; method: string }>;
  matches: Array<{ id: string; score: number; status: string }>;
  scores: CustomerScore[];
}

export interface CustomerEvent {
  id: string;
  event_type: string;
  occurred_at: string;
  channel: string | null;
  amount: number | null;
  source_system: string | null;
  event_key: string | null;
  attributes: Record<string, unknown>;
}

export interface CustomerSegment {
  id: string;
  name: string;
  description: string;
  method: string;
  definition: unknown;
  size: number;
  refreshed_at: string | null;
  created_at: string;
}

export interface CustomerSettings {
  auto_threshold: number;
  review_threshold: number;
  weights: Record<string, number>;
  survivorship: Record<string, string>;
  default_country_code: string;
  features_dataset_id: string | null;
  defaults: { auto_threshold: number; review_threshold: number; weights: Record<string, number>; survivorship: Record<string, string>; survivorship_rules: string[] };
}

export interface JobAccepted {
  job_id: string;
  run_id?: string;
  status_url?: string;
  events_url?: string;
}

export const customerKeys = {
  all: (org: string) => ["org", org, "customers"] as const,
  list: (org: string) => ["org", org, "customers", "list"] as const,
  detail: (org: string, id: string) => ["org", org, "customers", "detail", id] as const,
  timeline: (org: string, id: string) => ["org", org, "customers", "timeline", id] as const,
  scores: (org: string, id: string) => ["org", org, "customers", "scores", id] as const,
  sources: (org: string) => ["org", org, "customers", "sources"] as const,
  runs: (org: string) => ["org", org, "customers", "runs"] as const,
  matches: (org: string) => ["org", org, "customers", "matches"] as const,
  segments: (org: string) => ["org", org, "customers", "segments"] as const,
  members: (org: string, id: string) => ["org", org, "customers", "segments", id, "members"] as const,
  settings: (org: string) => ["org", org, "customers", "settings"] as const,
};

export function useCustomers(org: string, query: Query) {
  return useCursorList<CustomerProfile>(customerKeys.list(org), o(org), { query });
}

export function useCustomer(org: string, id: string) {
  return useQuery({ queryKey: customerKeys.detail(org, id), retry, queryFn: ({ signal }) => api.get<CustomerDetail>(`${o(org)}/${id}`, { signal }) });
}

export function useCustomerTimeline(org: string, id: string, eventType: string) {
  return useCursorList<CustomerEvent>(customerKeys.timeline(org, id), `${o(org)}/${id}/timeline`, { query: { event_type: eventType || undefined } });
}

export function useCustomerScores(org: string, id: string) {
  return useQuery({
    queryKey: customerKeys.scores(org, id),
    retry,
    queryFn: ({ signal }) => api.get<{ latest: CustomerScore[]; history: CustomerScore[] }>(`${o(org)}/${id}/scores`, { signal }),
  });
}

export function useCustomerSources(org: string, enabled = true) {
  return useQuery({ queryKey: customerKeys.sources(org), enabled, retry, queryFn: ({ signal }) => fetchAll<CustomerSource>(`${o(org)}/sources`, { signal }) });
}

export function useResolutionRuns(org: string) {
  return useCursorList<CustomerRun>(customerKeys.runs(org), `${o(org)}/resolution-runs`, { limit: 20 });
}

export function useCustomerMatches(org: string, status: string, enabled = true) {
  return useCursorList<IdentityMatch>(customerKeys.matches(org), `${o(org)}/matches`, { query: { status }, limit: 20, enabled });
}

export function useCustomerSegments(org: string, enabled = true) {
  return useQuery({ queryKey: customerKeys.segments(org), enabled, retry, queryFn: ({ signal }) => fetchAll<CustomerSegment>(`${o(org)}/segments`, { signal }) });
}

export function useSegmentMembers(org: string, id: string | null) {
  return useCursorList<CustomerProfile>(customerKeys.members(org, id ?? "-"), `${o(org)}/segments/${id}/members`, { enabled: !!id, limit: 25 });
}

export function useCustomerSettings(org: string) {
  return useQuery({ queryKey: customerKeys.settings(org), retry, queryFn: ({ signal }) => api.get<CustomerSettings>(`${o(org)}/settings`, { signal }) });
}

function useInvalidate(org: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: customerKeys.all(org) });
}

export interface CreateCustomerSource {
  dataset_id: string;
  kind: CustomerSourceKind;
  mapping: Record<string, string | string[]>;
  name?: string;
  priority?: number;
  link_source_id?: string;
}

export function useCreateCustomerSource(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (body: CreateCustomerSource) => api.post<CustomerSource>(`${o(org)}/sources`, body), onSuccess: inv });
}

export function useDeleteCustomerSource(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (id: string) => api.delete(`${o(org)}/sources/${id}`), onSuccess: inv });
}

export function useStartResolution(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: () => api.post<JobAccepted>(`${o(org)}/resolve`, {}), onSuccess: inv });
}

export function useDecideCustomerMatch(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: ({ id, decision, note }: { id: string; decision: "confirm" | "reject"; note?: string }) =>
      api.post<IdentityMatch>(`${o(org)}/matches/${id}/decision`, { decision, note: note || undefined }),
    onSuccess: inv,
  });
}

export function useCreateSegment(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: (body: { method: "rule"; name: string; description?: string; definition: SegmentDefinition } | { method: "rfm" }) =>
      api.post<unknown>(`${o(org)}/segments`, body),
    onSuccess: inv,
  });
}

export function useRefreshSegment(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (id: string) => api.post<CustomerSegment>(`${o(org)}/segments/${id}/refresh`, {}), onSuccess: inv });
}

export function useDeleteSegment(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (id: string) => api.delete(`${o(org)}/segments/${id}`), onSuccess: inv });
}

export function useStartScoring(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: (body: { score_type: "churn" | "clv"; model_id: string }) => api.post<JobAccepted>(`${o(org)}/scoring-runs`, body),
    onSuccess: inv,
  });
}

export function usePublishFeatures(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: () => api.post<{ dataset_id: string; version: number; status: string; job_id?: string }>(`${o(org)}/features/publish`, {}),
    onSuccess: inv,
  });
}

export function useUpdateCustomerSettings(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<Pick<CustomerSettings, "auto_threshold" | "review_threshold" | "weights" | "survivorship" | "default_country_code">>) =>
      api.patch<CustomerSettings>(`${o(org)}/settings`, body),
    onSuccess: (data) => qc.setQueryData(customerKeys.settings(org), data),
  });
}

/** Field lists of a source mapping (mirror `apps/customers/services.py`). */
export const PROFILE_FIELDS = ["external_id", "email", "phone", "name", "first_name", "last_name", "address", "dob", "created_at"] as const;
export const EVENT_FIELDS = ["customer_key", "occurred_at", "event_type_column", "amount", "channel", "event_id"] as const;
