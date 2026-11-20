/**
 * Which dashboard the org home page shows (pure). `is_default` is per OWNER in the backend (at most one per
 * owner, `apps/bi` Dashboard), and `GET /dashboards` also lists other members' shared dashboards, which may be
 * their owners' defaults. The caller's own default wins; otherwise the most recently updated shared default (an
 * org-wide home a teammate published); otherwise nothing, and the home page shows only its live KPIs.
 */

import type { DashboardSummary } from "@/lib/types";

export function pickHomeDashboard(list: DashboardSummary[] | null | undefined, userId: string | null | undefined): DashboardSummary | null {
  const items = list ?? [];
  const own = items.find((d) => d.is_default && (!d.owner_id || d.owner_id === userId));
  if (own) return own;
  const shared = items
    .filter((d) => d.is_default && d.is_shared && d.owner_id !== userId)
    .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
  return shared[0] ?? null;
}
