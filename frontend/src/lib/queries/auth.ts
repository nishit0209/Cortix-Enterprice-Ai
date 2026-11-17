"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isApiError } from "@/lib/api";
import type { LoginRequest, MeResponse, Org, SignupRequest, SignupResponse, User } from "@/lib/types";

export const authKeys = {
  me: ["me"] as const,
};

/** GET /me. Resolves to `null` (not an error) when the visitor is signed out. */
export function useMe(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: authKeys.me,
    enabled: options.enabled ?? true,
    staleTime: 60_000,
    retry: (count, err) => !(isApiError(err) && (err.status === 401 || err.status === 403)) && count < 2,
    queryFn: async ({ signal }) => {
      try {
        return await api.get<MeResponse>("/me", { signal, silent401: true });
      } catch (e) {
        if (isApiError(e) && (e.status === 401 || e.status === 403)) return null;
        throw e;
      }
    },
  });
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: LoginRequest) => api.post<{ user: User }>("/auth/login", body, { silent401: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: authKeys.me }),
  });
}

export function useSignup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SignupRequest) => api.post<SignupResponse>("/auth/signup", body, { silent401: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: authKeys.me }),
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<void>("/auth/logout", undefined, { silent401: true }),
    onSettled: () => {
      qc.clear();
    },
  });
}

export function useCreateOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string }) => api.post<Org>("/orgs", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: authKeys.me }),
  });
}
