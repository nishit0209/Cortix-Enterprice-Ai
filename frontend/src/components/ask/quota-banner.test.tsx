import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QuotaBanner } from "./quota-banner";
import { aiHealthOf, quotaBannerCopy, quotaOf } from "@/lib/ai/quota";
import { formatDuration } from "@/hooks/use-countdown";
import type { AiQuota, AiStatus } from "@/lib/types";

const NOW = new Date("2026-11-20T10:00:00Z").getTime();
const at = (ms: number) => new Date(NOW + ms).toISOString();

function status(over: Partial<AiStatus> = {}, quota?: Partial<AiQuota> | null): AiStatus {
  return {
    policy: "synthetic_only",
    tier: "free",
    configured: true,
    org_budget: { limit: 500, used: 10 },
    breaker: "closed",
    resets_at: at(14 * 3600_000),
    budget_resets_at: at(14 * 3600_000),
    quota: quota === null ? null : quota ? { state: "ok", resets_at: at(14 * 3600_000), used_today: 10, limit_today: 500, ...quota } : undefined,
    ...over,
  };
}

describe("QuotaBanner live countdown", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("counts down to resets_at every second and refetches once when it reaches zero", () => {
    const onReset = vi.fn();
    render(<QuotaBanner status={status({}, { state: "exhausted", resets_at: at(65_000), used_today: 500, limit_today: 500 })} onReset={onReset} />);
    const banner = screen.getByTestId("quota-banner");
    expect(banner).toHaveTextContent("Today's AI quota is used up");
    expect(banner).toHaveTextContent("500 of 500 AI requests used today.");
    expect(banner).toHaveTextContent("Resets in 1m 05s");

    act(() => void vi.advanceTimersByTime(5_000));
    expect(screen.getByTestId("quota-banner")).toHaveTextContent("Resets in 1m 00s");

    act(() => void vi.advanceTimersByTime(59_000));
    expect(screen.getByTestId("quota-banner")).toHaveTextContent("Resets in 1s");
    expect(onReset).not.toHaveBeenCalled();

    act(() => void vi.advanceTimersByTime(1_000));
    expect(screen.getByTestId("quota-banner")).toHaveTextContent("Resetting now…");
    expect(onReset).toHaveBeenCalledTimes(1);

    act(() => void vi.advanceTimersByTime(10_000));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("warns while the quota is low and stays hidden when it is fine or AI is not configured", () => {
    const { rerender } = render(<QuotaBanner status={status({}, { state: "low", used_today: 450, resets_at: at(2 * 3600_000 + 5 * 60_000) })} />);
    expect(screen.getByTestId("quota-banner")).toHaveTextContent("AI quota is running low");
    expect(screen.getByTestId("quota-banner")).toHaveTextContent("Resets in 2h 05m");

    rerender(<QuotaBanner status={status({}, { state: "ok" })} />);
    expect(screen.queryByTestId("quota-banner")).toBeNull();

    rerender(<QuotaBanner status={status({ configured: false }, { state: "exhausted" })} />);
    expect(screen.queryByTestId("quota-banner")).toBeNull();

    rerender(<QuotaBanner status={undefined} />);
    expect(screen.queryByTestId("quota-banner")).toBeNull();
  });
});

describe("quota state from /ai/status", () => {
  it("uses the W3 quota block as sent", () => {
    const q = quotaOf(status({}, { state: "low", used_today: 420, limit_today: 500, rate_limited: true, user: { used: 12, limit: 50 } }));
    expect(q).toMatchObject({ state: "low", used_today: 420, limit_today: 500, rate_limited: true, user: { used: 12, limit: 50 } });
  });

  it("derives the same states from org_budget / breaker on older payloads", () => {
    expect(quotaOf(status({ breaker: "open_day", resets_at: at(1000) }))).toMatchObject({ state: "exhausted", resets_at: at(1000) });
    expect(quotaOf(status({ org_budget: { limit: 100, used: 100 } }))?.state).toBe("exhausted");
    expect(quotaOf(status({ org_budget: { limit: 100, used: 80 } }))?.state).toBe("low");
    expect(quotaOf(status({ org_budget: { limit: 100, used: 79 } }))?.state).toBe("ok");
    expect(quotaOf(status({ org_budget: { limit: 0, used: 5 } }))?.state).toBe("ok");
    expect(quotaOf(undefined)).toBeNull();
  });

  it("summarizes health for the status pill", () => {
    expect(aiHealthOf(status({ configured: false }))).toEqual({ tone: "neutral", label: "Not configured" });
    expect(aiHealthOf(status({ breaker: "open_day" }, { state: "exhausted" })).label).toBe("Paused for today");
    expect(aiHealthOf(status({}, { state: "exhausted" })).label).toBe("Quota used up");
    expect(aiHealthOf(status({}, { state: "ok", rate_limited: true })).label).toBe("Cooling down");
    expect(aiHealthOf(status({}, { state: "low" })).tone).toBe("warning");
    expect(aiHealthOf(status({}, { state: "ok" }))).toEqual({ tone: "success", label: "Available" });
  });

  it("writes banner copy only for low or exhausted quotas", () => {
    expect(quotaBannerCopy({ state: "ok", resets_at: null, used_today: 1, limit_today: 2 })).toBeNull();
    expect(quotaBannerCopy({ state: "exhausted", resets_at: null, used_today: 2, limit_today: 2 }, false)).toBeNull();
    expect(quotaBannerCopy({ state: "exhausted", resets_at: null, used_today: 2, limit_today: 2 })?.tone).toBe("danger");
    expect(quotaBannerCopy({ state: "low", resets_at: null, used_today: 0, limit_today: 0 })?.text).toBe("Cached and builder answers don't count against it.");
  });

  it("formats the remaining time", () => {
    expect(formatDuration(3_725_000)).toBe("1h 02m");
    expect(formatDuration(65_000)).toBe("1m 05s");
    expect(formatDuration(999)).toBe("1s");
  });
});

describe("quota copy follows quota.scope (the budget that tripped)", () => {
  // The org has used 120 of its 500 requests; another budget is the one that is out or low.
  const scoped = (scope: AiQuota["scope"], state: "low" | "exhausted", over: Partial<AiStatus> = {}) =>
    status({ token_budget: { limit: 200_000, used: 200_000 }, user_budget: { limit: 50, used: 50 }, ...over }, {
      state,
      scope,
      used_today: 120,
      limit_today: 500,
      user: { used: state === "exhausted" ? 50 : 42, limit: 50 },
    });

  it("explains a used-up personal limit with the user's own numbers", () => {
    render(<QuotaBanner status={scoped("user", "exhausted")} />);
    const banner = screen.getByTestId("quota-banner");
    expect(banner).toHaveTextContent("You've used your AI requests for today");
    expect(banner).toHaveTextContent("You have used 50 of your 50 daily AI requests.");
    expect(banner).not.toHaveTextContent("120 of 500");
    expect(aiHealthOf(scoped("user", "exhausted"))).toEqual({ tone: "danger", label: "Your limit reached" });
  });

  it("names the token budget, and the platform quota, instead of the org request count", () => {
    const tokens = quotaBannerCopy(quotaOf(scoped("org_tokens", "exhausted")));
    expect(tokens).toMatchObject({ tone: "danger", title: "Today's AI token budget is used up" });
    expect(tokens?.text).toContain(`${(200_000).toLocaleString()} of ${(200_000).toLocaleString()} AI tokens used today.`);
    expect(tokens?.text).not.toContain("120 of 500");
    expect(aiHealthOf(scoped("org_tokens", "exhausted")).label).toBe("Token budget used up");

    const platform = quotaBannerCopy(quotaOf(scoped("platform", "exhausted")));
    expect(platform).toMatchObject({ tone: "danger", title: "Gemini's daily capacity is reached" });
    expect(platform?.text).not.toContain("120 of 500");
    expect(aiHealthOf(scoped("platform", "exhausted")).label).toBe("Paused for today");
  });

  it("says which budget is running low", () => {
    expect(quotaBannerCopy(quotaOf(scoped("user", "low")))).toMatchObject({
      tone: "warning",
      title: "Your AI requests are running low",
      text: "You have used 42 of your 50 daily AI requests. Cached and builder answers don't count against it.",
    });
    expect(quotaBannerCopy(quotaOf(scoped("org_tokens", "low")))?.title).toBe("AI token budget is running low");
    expect(quotaBannerCopy(quotaOf(scoped("platform", "low")))?.title).toBe("Gemini's daily capacity is running low");
    expect(quotaBannerCopy(quotaOf(scoped("org_requests", "low")))?.text).toContain("120 of 500 AI requests used today.");
    // No scope (older W3 payloads): the org request budget, as before.
    expect(quotaBannerCopy(quotaOf(scoped(null, "exhausted")))?.title).toBe("Today's AI quota is used up");
  });

  it("ignores an unknown scope and derives the scope on older payloads", () => {
    expect(quotaOf(status({}, { state: "low", scope: "galaxy" as AiQuota["scope"] }))?.scope).toBeNull();
    expect(quotaOf(status({ breaker: "open_day" }))?.scope).toBe("platform");
    expect(quotaOf(status({ org_budget: { limit: 500, used: 120 }, user_budget: { limit: 50, used: 50 } }))).toMatchObject({
      state: "exhausted",
      scope: "user",
    });
    expect(quotaOf(status({ org_budget: { limit: 500, used: 120 }, token_budget: { limit: 1000, used: 850 } }))).toMatchObject({
      state: "low",
      scope: "org_tokens",
    });
    expect(quotaOf(status({ org_budget: { limit: 100, used: 100 } }))?.scope).toBe("org_requests");
  });
});
