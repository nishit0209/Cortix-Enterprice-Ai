"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, cursorFrom, unwrapList } from "@/lib/api";
import type {
  ActivityEvent,
  AiPolicyState,
  AiStatus,
  AuditEvent,
  ConsentText,
  DataStats,
  GrantConsentRequest,
  Insight,
  Invitation,
  Member,
  Org,
  OrgRoleKey,
  Paginated,
  Role,
} from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export const orgKeys = {
  org: (org: string) => ["org", org] as const,
  members: (org: string) => ["org", org, "members"] as const,
  invitations: (org: string) => ["org", org, "invitations"] as const,
  roles: (org: string) => ["org", org, "roles"] as const,
  aiPolicy: (org: string) => ["org", org, "ai-policy"] as const,
  consentText: (org: string) => ["org", org, "ai-policy", "consent-text"] as const,
  aiStatus: (org: string) => ["org", org, "ai-status"] as const,
  activity: (org: string) => ["org", org, "activity"] as const,
  audit: (org: string, filters: Record<string, string>) => ["org", org, "audit", filters] as const,
  dataStats: (org: string) => ["org", org, "data-stats"] as const,
  insights: (org: string) => ["org", org, "insights"] as const,
};

/* ---------------- org ---------------- */

export function useOrg(org: string | undefined) {
  return useQuery({
    queryKey: orgKeys.org(org ?? ""),
    enabled: !!org,
    staleTime: 60_000,
    queryFn: ({ signal }) => api.get<Org>(o(org!), { signal }),
  });
}

export function useUpdateOrg(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name?: string; settings?: Record<string, unknown> }) => api.patch<Org>(o(org), body),
    onSuccess: (data) => {
      qc.setQueryData(orgKeys.org(org), data);
      void qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

/** Permission check against the org's `permissions` codenames. Undefined org = not yet known. */
export function hasPerm(orgData: Org | undefined, perm: string | string[]): boolean {
  if (!orgData) return false;
  const perms = Array.isArray(perm) ? perm : [perm];
  return perms.every((p) => orgData.permissions?.includes(p));
}

/* ---------------- members / invitations / roles ---------------- */

export function useMembers(org: string) {
  return useQuery({
    queryKey: orgKeys.members(org),
    queryFn: async ({ signal }) => unwrapList(await api.get<Member[] | Paginated<Member>>(`${o(org)}/members`, { signal, query: { limit: 200 } })),
  });
}

export function useInviteMember(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; role: OrgRoleKey }) => api.post<Member | Invitation>(`${o(org)}/members`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: orgKeys.members(org) });
      void qc.invalidateQueries({ queryKey: orgKeys.invitations(org) });
    },
  });
}

export function useUpdateMember(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, role }: { id: string; role: OrgRoleKey }) => api.patch<Member>(`${o(org)}/members/${id}`, { role }),
    onSuccess: () => qc.invalidateQueries({ queryKey: orgKeys.members(org) }),
  });
}

export function useRemoveMember(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/members/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: orgKeys.members(org) }),
  });
}

export function useInvitations(org: string, enabled = true) {
  return useQuery({
    queryKey: orgKeys.invitations(org),
    enabled,
    queryFn: async ({ signal }) =>
      unwrapList(await api.get<Invitation[] | Paginated<Invitation>>(`${o(org)}/invitations`, { signal })),
  });
}

export function useRevokeInvitation(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/invitations/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: orgKeys.invitations(org) }),
  });
}

export function useRoles(org: string) {
  return useQuery({
    queryKey: orgKeys.roles(org),
    staleTime: 5 * 60_000,
    queryFn: async ({ signal }) => unwrapList(await api.get<Role[] | Paginated<Role>>(`${o(org)}/roles`, { signal })),
  });
}

/* ---------------- AI policy ---------------- */

export function useAiPolicy(org: string) {
  return useQuery({
    queryKey: orgKeys.aiPolicy(org),
    queryFn: ({ signal }) => api.get<AiPolicyState>(`${o(org)}/ai-policy`, { signal }),
  });
}

export function useConsentText(org: string, enabled: boolean) {
  return useQuery({
    queryKey: orgKeys.consentText(org),
    enabled,
    staleTime: 10 * 60_000,
    queryFn: ({ signal }) => api.get<ConsentText>(`${o(org)}/ai-policy/consent-text`, { signal }),
  });
}

function invalidatePolicy(qc: ReturnType<typeof useQueryClient>, org: string) {
  void qc.invalidateQueries({ queryKey: orgKeys.aiPolicy(org) });
  void qc.invalidateQueries({ queryKey: orgKeys.aiStatus(org) });
  void qc.invalidateQueries({ queryKey: orgKeys.org(org) });
  void qc.invalidateQueries({ queryKey: orgKeys.activity(org) });
}

export function useGrantConsent(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: GrantConsentRequest) => api.post<unknown>(`${o(org)}/ai-policy/consents`, body),
    onSuccess: () => invalidatePolicy(qc, org),
  });
}

export function useRevokeConsent(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<unknown>(`${o(org)}/ai-policy/revoke`),
    onSuccess: () => invalidatePolicy(qc, org),
  });
}

export function useAiStatus(org: string | undefined) {
  return useQuery({
    queryKey: orgKeys.aiStatus(org ?? ""),
    enabled: !!org,
    refetchInterval: 60_000,
    retry: 1,
    queryFn: ({ signal }) => api.get<AiStatus>(`${o(org!)}/ai/status`, { signal }),
  });
}

/* ---------------- activity / audit ---------------- */

export function useActivity(org: string | undefined, options: { refetchInterval?: number } = {}) {
  return useQuery({
    queryKey: orgKeys.activity(org ?? ""),
    enabled: !!org,
    refetchInterval: options.refetchInterval ?? 60_000,
    retry: 1,
    queryFn: async ({ signal }) =>
      unwrapList(await api.get<ActivityEvent[] | Paginated<ActivityEvent>>(`${o(org!)}/activity`, { signal })),
  });
}

export function useAuditEvents(org: string, filters: { action?: string; target_type?: string }) {
  const clean: Record<string, string> = {};
  if (filters.action) clean.action = filters.action;
  if (filters.target_type) clean.target_type = filters.target_type;
  return useInfiniteQuery({
    queryKey: orgKeys.audit(org, clean),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) => {
      const res = await api.get<Paginated<AuditEvent> | AuditEvent[]>(`${o(org)}/audit-events`, {
        signal,
        query: { ...clean, limit: 100, cursor: pageParam ?? undefined },
      });
      if (Array.isArray(res)) return { items: res, next_cursor: null, previous_cursor: null } as Paginated<AuditEvent>;
      return res;
    },
    getNextPageParam: (last) => cursorFrom(last.next_cursor),
  });
}

/* ---------------- dashboard data ---------------- */

export function useDataStats(org: string) {
  return useQuery({
    queryKey: orgKeys.dataStats(org),
    retry: 1,
    queryFn: ({ signal }) => api.get<DataStats>(`${o(org)}/data-stats`, { signal }),
  });
}

export function useInsights(org: string) {
  return useQuery({
    queryKey: orgKeys.insights(org),
    retry: 1,
    queryFn: async ({ signal }) => unwrapList(await api.get<Insight[] | Paginated<Insight>>(`${o(org)}/insights`, { signal })),
  });
}
