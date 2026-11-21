"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { DateRange, UsageMetric, UsageReport } from "@/lib/usage";

/** GET /orgs/{org}/usage?from&to&metric (`org.view`; per-user breakdown only with `members.view`). */
export function useOrgUsage(org: string, range: DateRange, metric: UsageMetric | null, enabled = true) {
  return useQuery({
    queryKey: ["org", org, "usage", range.from, range.to, metric ?? "all"] as const,
    enabled,
    retry: false,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) =>
      api.get<UsageReport>(`/orgs/${encodeURIComponent(org)}/usage`, { signal, query: { from: range.from, to: range.to, metric: metric ?? undefined } }),
  });
}
