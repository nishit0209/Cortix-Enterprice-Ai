import { describe, expect, it } from "vitest";
import type { AiUsage } from "@/lib/types";
import { aiByModel, aiLookbackDays, breakdown, halfOverHalf, isUnavailable, mergeDaily, metricOf, presetRange, rangeDays, validateRange, type UsageReport } from "./usage";

const NOW = new Date("2026-11-21T15:00:00Z");

describe("usage date ranges", () => {
  it("builds inclusive UTC presets", () => {
    expect(presetRange(7, NOW)).toEqual({ from: "2026-11-15", to: "2026-11-21" });
    expect(rangeDays(presetRange(30, NOW))).toBe(30);
    expect(presetRange(1, NOW)).toEqual({ from: "2026-11-21", to: "2026-11-21" });
  });

  it("validates like the server (order, 366-day cap, completeness)", () => {
    expect(validateRange({ from: "2026-11-01", to: "2026-11-21" }, NOW)).toBeNull();
    expect(validateRange({ from: "2026-11-22", to: "2026-11-21" }, NOW)).toMatch(/after/);
    expect(validateRange({ from: "2025-11-20", to: "2026-11-21" }, NOW)).toMatch(/366/);
    expect(validateRange({ from: "2025-11-21", to: "2026-11-21" }, NOW)).toBeNull();
    expect(validateRange({ from: "", to: "2026-11-21" }, NOW)).toMatch(/both/);
    expect(validateRange({ from: "2026-12-01", to: "2026-12-02" }, NOW)).toMatch(/future/);
  });

  it("asks /ai/usage for enough days to cover the start, capped at 90", () => {
    expect(aiLookbackDays({ from: "2026-11-15", to: "2026-11-21" }, NOW)).toBe(7);
    expect(aiLookbackDays({ from: "2025-01-01", to: "2026-11-21" }, NOW)).toBe(90);
  });
});

describe("usage series", () => {
  it("merges per-day series, filling missing keys with 0 and sorting by day", () => {
    expect(
      mergeDaily({
        queries: [
          { day: "2026-11-02", count: 3 },
          { day: "2026-11-01", count: 1 },
        ],
        runs: [{ day: "2026-11-02", count: 5 }],
        missing: undefined,
      }),
    ).toEqual([
      { day: "2026-11-01", queries: 1, runs: 0, missing: 0 },
      { day: "2026-11-02", queries: 3, runs: 5, missing: 0 },
    ]);
  });

  it("sorts breakdowns largest first and drops zeros", () => {
    expect(breakdown({ allowed: 3, rejected: 0, blocked: 5 })).toEqual([
      { name: "blocked", value: 5 },
      { name: "allowed", value: 3 },
    ]);
    expect(breakdown(undefined)).toEqual([]);
  });

  it("computes a half-over-half trend, null without baseline", () => {
    expect(halfOverHalf([1, 1, 2, 2].map((count, i) => ({ day: String(i), count })))).toBe(1);
    expect(halfOverHalf([0, 0, 2, 2].map((count, i) => ({ day: String(i), count })))).toBeNull();
    expect(halfOverHalf([])).toBeNull();
  });

  it("aggregates AI usage per model within the range only", () => {
    const usage = {
      range: { from: "2026-11-01", to: "2026-11-21", days: 21, timezone: "America/Los_Angeles" },
      totals: { requests: 0, input_tokens: 0, output_tokens: 0, cached_tokens: 0, blocked: 0, errors: 0 },
      daily: [
        { day: "2026-11-10", model: "gemini-flash", requests: 4, input_tokens: 100, output_tokens: 50, blocked: 1, errors: 0 },
        { day: "2026-11-11", model: "gemini-flash", requests: 2, input_tokens: 10, output_tokens: 5, blocked: 0, errors: 1 },
        { day: "2026-11-11", model: "gemini-pro", requests: 1, input_tokens: 1, output_tokens: 1, blocked: 0, errors: 0 },
        { day: "2026-11-01", model: "gemini-pro", requests: 9, input_tokens: 1, output_tokens: 1, blocked: 0, errors: 0 },
      ],
    } satisfies AiUsage;
    expect(aiByModel(usage, { from: "2026-11-05", to: "2026-11-21" })).toEqual([
      { model: "gemini-flash", requests: 6, tokens: 165, blocked: 1, errors: 1 },
      { model: "gemini-pro", requests: 1, tokens: 2, blocked: 0, errors: 0 },
    ]);
  });

  it("treats {available:false} metrics as unavailable, not zero", () => {
    const r: UsageReport = {
      range: { from: "2026-11-01", to: "2026-11-02", days: 2, timezone: "UTC" },
      generated_at: "2026-11-02T00:00:00Z",
      metrics: { documents: { available: false }, datasets: { total: 2, by_status: { ready: 2 }, created: 1, per_day: [] } },
    };
    expect(metricOf(r, "documents")).toBeNull();
    expect(isUnavailable(r, "documents")).toBe(true);
    expect(metricOf(r, "datasets")?.total).toBe(2);
    expect(isUnavailable(r, "datasets")).toBe(false);
    expect(metricOf(r, "queries")).toBeNull();
  });
});
