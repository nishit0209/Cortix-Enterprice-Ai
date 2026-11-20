/**
 * AI quota state for the UI (pure). `GET /orgs/{org}/ai/status` carries
 * `quota: {state: ok|low|exhausted, resets_at, used_today, limit_today}` (W3). Older payloads only have
 * `org_budget` / `breaker` / `budget_resets_at`; the same state is derived from those real numbers.
 */

import type { AiQuota, AiStatus } from "@/lib/types";

/** Share of the daily budget at which the quota counts as "low" when the backend does not say. */
export const LOW_QUOTA_RATIO = 0.8;

export function quotaOf(s: AiStatus | null | undefined): AiQuota | null {
  if (!s) return null;
  if (s.quota && typeof s.quota === "object" && s.quota.state) {
    const user = s.quota.user;
    return {
      state: s.quota.state,
      resets_at: s.quota.resets_at ?? s.budget_resets_at ?? s.resets_at ?? null,
      used_today: Number(s.quota.used_today ?? 0),
      limit_today: Number(s.quota.limit_today ?? 0),
      rate_limited: !!s.quota.rate_limited,
      user: user && typeof user === "object" ? { used: Number(user.used ?? 0), limit: Number(user.limit ?? 0) } : null,
    };
  }
  const limit = Number(s.org_budget?.limit ?? 0);
  const used = Number(s.org_budget?.used ?? 0);
  const resets = s.budget_resets_at ?? s.resets_at ?? null;
  if (s.breaker === "open_day") return { state: "exhausted", resets_at: s.resets_at ?? resets, used_today: used, limit_today: limit };
  if (limit > 0 && used >= limit) return { state: "exhausted", resets_at: resets, used_today: used, limit_today: limit };
  if (limit > 0 && used / limit >= LOW_QUOTA_RATIO) return { state: "low", resets_at: resets, used_today: used, limit_today: limit };
  return { state: "ok", resets_at: resets, used_today: used, limit_today: limit };
}

export type AiHealthTone = "success" | "warning" | "danger" | "neutral";

/** One-word health for the status pill, from the W3 `/ai/status` fields. */
export function aiHealthOf(s: AiStatus | null | undefined): { tone: AiHealthTone; label: string } {
  if (!s) return { tone: "neutral", label: "Unknown" };
  if (!s.configured) return { tone: "neutral", label: "Not configured" };
  const q = quotaOf(s);
  if (q?.state === "exhausted") return { tone: "danger", label: s.breaker === "open_day" ? "Paused for today" : "Quota used up" };
  if (s.breaker === "open_minute" || q?.rate_limited) return { tone: "warning", label: "Cooling down" };
  if (q?.state === "low") return { tone: "warning", label: "Quota low" };
  return { tone: "success", label: "Available" };
}

/** Banner copy for the Ask AI workspace (null when the quota is fine). */
export function quotaBannerCopy(q: AiQuota | null, configured = true): { tone: "warning" | "danger"; title: string; text: string } | null {
  if (!q || !configured || q.state === "ok") return null;
  const usage = q.limit_today > 0 ? `${q.used_today.toLocaleString()} of ${q.limit_today.toLocaleString()} AI requests used today.` : "";
  if (q.state === "exhausted") {
    return {
      tone: "danger",
      title: "Today's AI quota is used up",
      text: `${usage} Answers from the semantic cache and the query builder still work; Gemini-generated SQL resumes when the quota resets.`.trim(),
    };
  }
  return { tone: "warning", title: "AI quota is running low", text: `${usage} Cached and builder answers don't count against it.`.trim() };
}
