/**
 * AI quota state for the UI (pure). `GET /orgs/{org}/ai/status` carries
 * `quota: {state: ok|low|exhausted, scope, resets_at, used_today, limit_today, user?}` (W3). `scope` names the budget
 * that decided the state: `platform` (the shared Gemini quota or the day breaker), `org_requests`, `org_tokens` or
 * `user`. `used_today` / `limit_today` are always the org's REQUEST budget, so the copy must follow `scope`.
 * Older payloads only have `org_budget` / `token_budget` / `user_budget` / `breaker` / `budget_resets_at`; the same
 * state and scope are derived from those real numbers.
 */

import type { AiQuota, AiQuotaScope, AiStatus } from "@/lib/types";

/** Share of the daily budget at which the quota counts as "low" when the backend does not say. */
export const LOW_QUOTA_RATIO = 0.8;

const SCOPES: readonly AiQuotaScope[] = ["platform", "org_requests", "org_tokens", "user"];

type Budget = { used: number; limit: number };

function budgetOf(b: { used?: unknown; limit?: unknown } | null | undefined): Budget | null {
  return b && typeof b === "object" ? { used: Number(b.used ?? 0), limit: Number(b.limit ?? 0) } : null;
}

function scopeOf(v: unknown): AiQuotaScope | null {
  return SCOPES.includes(v as AiQuotaScope) ? (v as AiQuotaScope) : null;
}

export function quotaOf(s: AiStatus | null | undefined): AiQuota | null {
  if (!s) return null;
  const tokens = budgetOf(s.token_budget);
  if (s.quota && typeof s.quota === "object" && s.quota.state) {
    return {
      state: s.quota.state,
      resets_at: s.quota.resets_at ?? s.budget_resets_at ?? s.resets_at ?? null,
      used_today: Number(s.quota.used_today ?? 0),
      limit_today: Number(s.quota.limit_today ?? 0),
      scope: scopeOf(s.quota.scope),
      rate_limited: !!s.quota.rate_limited,
      user: budgetOf(s.quota.user) ?? budgetOf(s.user_budget),
      tokens,
    };
  }
  const limit = Number(s.org_budget?.limit ?? 0);
  const used = Number(s.org_budget?.used ?? 0);
  const user = budgetOf(s.user_budget);
  const resets = s.budget_resets_at ?? s.resets_at ?? null;
  const base = { resets_at: resets, used_today: used, limit_today: limit, user, tokens };
  if (s.breaker === "open_day") return { ...base, state: "exhausted", scope: "platform", resets_at: s.resets_at ?? resets };
  // Same order the backend reports a refusal in; a limit of 0 means "no limit" on these older payloads.
  const budgets: [AiQuotaScope, Budget | null][] = [
    ["org_requests", { used, limit }],
    ["org_tokens", tokens],
    ["user", user],
  ];
  const out = budgets.find(([, b]) => b && b.limit > 0 && b.used >= b.limit);
  if (out) return { ...base, state: "exhausted", scope: out[0] };
  const low = budgets.find(([, b]) => b && b.limit > 0 && b.used / b.limit >= LOW_QUOTA_RATIO);
  if (low) return { ...base, state: "low", scope: low[0] };
  return { ...base, state: "ok", scope: null };
}

export type AiHealthTone = "success" | "warning" | "danger" | "neutral";

/** One-word health for the status pill, from the W3 `/ai/status` fields. */
export function aiHealthOf(s: AiStatus | null | undefined): { tone: AiHealthTone; label: string } {
  if (!s) return { tone: "neutral", label: "Unknown" };
  if (!s.configured) return { tone: "neutral", label: "Not configured" };
  const q = quotaOf(s);
  if (q?.state === "exhausted") {
    const label =
      s.breaker === "open_day" || q.scope === "platform"
        ? "Paused for today"
        : q.scope === "user"
          ? "Your limit reached"
          : q.scope === "org_tokens"
            ? "Token budget used up"
            : "Quota used up";
    return { tone: "danger", label };
  }
  if (s.breaker === "open_minute" || q?.rate_limited) return { tone: "warning", label: "Cooling down" };
  if (q?.state === "low") return { tone: "warning", label: q.scope === "user" ? "Your quota low" : "Quota low" };
  return { tone: "success", label: "Available" };
}

/** True when the quota state comes from the org's request budget (the numbers in `used_today` / `limit_today`). */
export function isOrgRequestScope(q: AiQuota | null | undefined): boolean {
  return !!q && (q.scope === "org_requests" || q.scope == null);
}

const STILL_WORKS = "Answers from the semantic cache and the query builder still work;";
const NOT_COUNTED = "Cached and builder answers don't count against it.";

/** Banner copy for the Ask AI workspace (null when the quota is fine). The reason follows `quota.scope`. */
export function quotaBannerCopy(q: AiQuota | null, configured = true): { tone: "warning" | "danger"; title: string; text: string } | null {
  if (!q || !configured || q.state === "ok") return null;
  const n = (x: number) => x.toLocaleString();
  const exhausted = q.state === "exhausted";
  const copy = (title: string, text: string) => ({ tone: exhausted ? ("danger" as const) : ("warning" as const), title, text: text.trim() });

  if (q.scope === "user") {
    const usage = q.user && q.user.limit > 0 ? `You have used ${n(q.user.used)} of your ${n(q.user.limit)} daily AI requests.` : "";
    return exhausted
      ? copy("You've used your AI requests for today", `${usage} ${STILL_WORKS} your Gemini requests resume when your quota resets.`)
      : copy("Your AI requests are running low", `${usage} ${NOT_COUNTED}`);
  }
  if (q.scope === "org_tokens") {
    const usage = q.tokens && q.tokens.limit > 0 ? `${n(q.tokens.used)} of ${n(q.tokens.limit)} AI tokens used today.` : "";
    return exhausted
      ? copy("Today's AI token budget is used up", `${usage} ${STILL_WORKS} Gemini-generated SQL resumes when the budget resets.`)
      : copy("AI token budget is running low", `${usage} ${NOT_COUNTED}`);
  }
  if (q.scope === "platform") {
    return exhausted
      ? copy(
          "Gemini's daily capacity is reached",
          `The shared daily Gemini quota is used up. ${STILL_WORKS} Gemini-generated SQL resumes when it resets.`,
        )
      : copy("Gemini's daily capacity is running low", `Most of the shared daily Gemini quota is used. ${NOT_COUNTED}`);
  }
  const usage = q.limit_today > 0 ? `${n(q.used_today)} of ${n(q.limit_today)} AI requests used today.` : "";
  return exhausted
    ? copy("Today's AI quota is used up", `${usage} ${STILL_WORKS} Gemini-generated SQL resumes when the quota resets.`)
    : copy("AI quota is running low", `${usage} ${NOT_COUNTED}`);
}
