"use client";

/**
 * Governance queries (API_SPECIFICATION Phase 5; `apps/governance` views). Every route needs
 * `governance.policy.edit`, except retention (`governance.retention.edit`) and access history (`audit.view`).
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, cursorFrom, unwrapList } from "@/lib/api";
import type { WirePredicate } from "@/lib/governance/conditions";
import type { Paginated } from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export type PrincipalType = "everyone" | "role" | "user";
export type Privilege = "read" | "query" | "export" | "admin";
export type ColumnAction = "deny" | "mask_null" | "mask_hash" | "mask_partial";

export interface DatasetAcl {
  id: string;
  dataset_id: string;
  principal_type: "role" | "user";
  principal_id: string;
  privilege: Privilege;
  created_by: string | null;
  created_at: string;
}

export interface ColumnPolicy {
  id: string;
  dataset_id: string;
  column_name: string;
  principal_type: PrincipalType;
  principal_id: string | null;
  action: ColumnAction;
  params: { keep_last?: number };
  exempt_role_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface RowPolicy {
  id: string;
  dataset_id: string;
  name: string;
  principal_type: PrincipalType;
  principal_id: string | null;
  predicate: WirePredicate;
  exempt_role_ids: string[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface PolicyPreview {
  access: boolean;
  policy: {
    tables: Record<string, string>;
    allowed_columns: Record<string, string[]>;
    column_masks: Array<{ table: string; column: string; kind: string; keep_last: number | null }>;
    row_predicates: Record<string, Array<{ col: string; op: string; value?: unknown }>>;
    row_cap: number | null;
    byte_cap: number | null;
    timeout_s: number | null;
    fingerprint: string;
  } | null;
}

/** Result of `POST …/policy-preview {sample: true}` (`apps/bi/policy_sample.sample_as`). */
export type PolicySample =
  | {
      ok: true;
      table?: string;
      columns: Array<{ name: string; type?: string; masked?: boolean }>;
      rows: unknown[][];
      row_count: number;
      truncated: boolean;
      executed_sql?: string;
      query_audit_id?: string;
    }
  | { ok: false; code: string; detail: string; query_audit_id?: string };

export interface PolicyPreviewWithSample extends PolicyPreview {
  sample?: PolicySample;
}

export type RetentionAction = "delete" | "archive";
export type RetentionMode = "dry_run" | "enforce";

export interface RetentionDryRun {
  cutoff: string;
  matched: number;
  action: RetentionAction;
  at: string;
  legal_hold: boolean;
}

export interface RetentionApplyResult {
  cutoff: string;
  affected: number;
  action: RetentionAction;
  at: string;
  trigger: string;
  skipped?: string;
}

/** `apps/governance` RetentionPolicySerializer (W4 as built). */
export interface RetentionPolicy {
  id: string;
  name: string;
  target: string;
  retain_days: number;
  action: RetentionAction;
  mode: RetentionMode;
  is_active: boolean;
  last_dry_run_at: string | null;
  last_dry_run: RetentionDryRun | null;
  last_applied_at: string | null;
  last_result: RetentionApplyResult | null;
  created_by?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface RetentionTarget {
  key: string;
  label: string;
  description: string;
  actions: RetentionAction[];
}

export interface RetentionTargets {
  items: RetentionTarget[];
  /** Append-only evidence that retention never touches. */
  never: string[];
}

export interface AccessHistoryRow {
  id: string;
  occurred_at: string;
  user_id: string | null;
  user_email: string | null;
  api_key_id: string | null;
  asset_type: string;
  asset_id: string;
  access_type: string;
  query_audit_id: string | null;
  metadata: Record<string, unknown>;
}

export const govKeys = {
  acl: (org: string, ds: string) => ["org", org, "gov", ds, "acl"] as const,
  columns: (org: string, ds: string) => ["org", org, "gov", ds, "column-policies"] as const,
  rows: (org: string, ds: string) => ["org", org, "gov", ds, "row-policies"] as const,
  attributes: (org: string, user: string) => ["org", org, "gov", "attributes", user] as const,
  retention: (org: string) => ["org", org, "gov", "retention"] as const,
  history: (org: string, f: Record<string, string>) => ["org", org, "gov", "access-history", f] as const,
};

function useGovList<T>(key: readonly unknown[], path: string, enabled: boolean) {
  return useQuery({
    queryKey: key,
    enabled,
    retry: false,
    queryFn: async ({ signal }) => unwrapList(await api.get<T[] | Paginated<T>>(path, { signal, query: { limit: 200 } })),
  });
}

export const useDatasetAcl = (org: string, ds: string | null) => useGovList<DatasetAcl>(govKeys.acl(org, ds ?? ""), `${o(org)}/datasets/${ds}/acl`, !!ds);
export const useColumnPolicies = (org: string, ds: string | null) =>
  useGovList<ColumnPolicy>(govKeys.columns(org, ds ?? ""), `${o(org)}/datasets/${ds}/column-policies`, !!ds);
export const useRowPolicies = (org: string, ds: string | null) => useGovList<RowPolicy>(govKeys.rows(org, ds ?? ""), `${o(org)}/datasets/${ds}/row-policies`, !!ds);

/** Create / update / delete for one dataset-scoped governance collection. */
export function useGovMutations<TBody>(org: string, ds: string, kind: "acl" | "column-policies" | "row-policies") {
  const qc = useQueryClient();
  const key = kind === "acl" ? govKeys.acl(org, ds) : kind === "column-policies" ? govKeys.columns(org, ds) : govKeys.rows(org, ds);
  const base = `${o(org)}/datasets/${ds}/${kind}`;
  const invalidate = () => qc.invalidateQueries({ queryKey: key, exact: true });
  return {
    create: useMutation({ mutationFn: (body: TBody) => api.post<unknown>(base, body), onSuccess: invalidate }),
    update: useMutation({ mutationFn: ({ id, body }: { id: string; body: Partial<TBody> }) => api.patch<unknown>(`${base}/${id}`, body), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: (id: string) => api.delete(`${base}/${id}`), onSuccess: invalidate }),
  };
}

export function usePolicyPreview(org: string) {
  return useMutation({
    mutationFn: ({ datasetId, userId, privilege, sample = false }: { datasetId: string; userId: string; privilege: Privilege; sample?: boolean }) =>
      api.post<PolicyPreviewWithSample>(`${o(org)}/datasets/${datasetId}/policy-preview`, sample ? { user_id: userId, privilege, sample: true, limit: 20 } : { user_id: userId, privilege }),
  });
}

export function useUserAttributes(org: string, userId: string | null) {
  return useQuery({
    queryKey: govKeys.attributes(org, userId ?? ""),
    enabled: !!userId,
    retry: false,
    queryFn: ({ signal }) => api.get<{ user_id: string; attributes: Record<string, string[]> }>(`${o(org)}/users/${userId}/attributes`, { signal }),
  });
}

export function useSaveUserAttributes(org: string, userId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (attributes: Record<string, string[]>) => api.put<{ user_id: string; attributes: Record<string, string[]> }>(`${o(org)}/users/${userId}/attributes`, { attributes }),
    onSuccess: (data) => qc.setQueryData(govKeys.attributes(org, userId), data),
  });
}

/* ---------------- retention ---------------- */

export function useRetentionPolicies(org: string, enabled = true) {
  return useQuery({
    queryKey: govKeys.retention(org),
    enabled,
    retry: false,
    queryFn: async ({ signal }) => unwrapList(await api.get<RetentionPolicy[] | Paginated<RetentionPolicy>>(`${o(org)}/retention-policies`, { signal, query: { limit: 200 } })),
  });
}

export function useRetentionTargets(org: string, enabled = true) {
  return useQuery({
    queryKey: ["org", org, "gov", "retention-targets"] as const,
    enabled,
    retry: false,
    staleTime: 5 * 60_000,
    queryFn: ({ signal }) => api.get<RetentionTargets>(`${o(org)}/retention-targets`, { signal }),
  });
}

export type RetentionCreate = Pick<RetentionPolicy, "name" | "target" | "retain_days" | "action">;
export type RetentionPatch = Partial<Pick<RetentionPolicy, "name" | "retain_days" | "action" | "mode" | "is_active">>;

export function useRetentionMutations(org: string) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: govKeys.retention(org), exact: true });
  const base = `${o(org)}/retention-policies`;
  return {
    create: useMutation({ mutationFn: (body: RetentionCreate) => api.post<RetentionPolicy>(base, body), onSuccess: invalidate }),
    update: useMutation({ mutationFn: ({ id, body }: { id: string; body: RetentionPatch }) => api.patch<RetentionPolicy>(`${base}/${id}`, body), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: (id: string) => api.delete(`${base}/${id}`), onSuccess: invalidate }),
    /** Dry run: counts what the policy would delete/archive now and records it on the policy. Nothing is changed. */
    dryRun: useMutation({
      mutationFn: (id: string) => api.post<{ policy: RetentionPolicy; dry_run: RetentionDryRun }>(`${base}/${id}/dry-run`, {}),
      onSuccess: invalidate,
    }),
    /** Deletes/archives the matched data now (needs a prior dry run; skipped under legal hold). */
    apply: useMutation({
      mutationFn: (id: string) => api.post<{ policy: RetentionPolicy; result: RetentionApplyResult }>(`${base}/${id}/apply`, { confirm: true }),
      onSuccess: invalidate,
    }),
  };
}

/* ---------------- access history ---------------- */

export function useAccessHistory(org: string, filters: Record<string, string>) {
  const clean = Object.fromEntries(Object.entries(filters).filter(([, v]) => !!v));
  return useInfiniteQuery({
    queryKey: govKeys.history(org, clean),
    initialPageParam: null as string | null,
    retry: false,
    queryFn: async ({ pageParam, signal }) => {
      const res = await api.get<Paginated<AccessHistoryRow> | AccessHistoryRow[]>(`${o(org)}/access-history`, {
        signal,
        query: { ...clean, limit: 100, cursor: pageParam ?? undefined },
      });
      if (Array.isArray(res)) return { items: res, next_cursor: null, previous_cursor: null } as Paginated<AccessHistoryRow>;
      return res;
    },
    getNextPageParam: (last) => cursorFrom(last.next_cursor),
  });
}
