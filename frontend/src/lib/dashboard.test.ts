import { describe, expect, it } from "vitest";
import type { DataStats } from "./types";
import { pctChange, previousStats } from "./dashboard";

const base: DataStats = { datasets: 10, rows_total: 100, avg_quality: 0.8, pipelines_active: 2, runs_7d: { succeeded: 1, failed: 0, total: 1 }, storage_bytes: 0 };

describe("previousStats", () => {
  it("is null when the API sends no previous values", () => {
    expect(previousStats(base)).toBeNull();
    expect(previousStats({ ...base, datasets_prev: null, avg_quality_prev: null })).toBeNull();
    expect(previousStats(undefined)).toBeNull();
  });
  it("prefers the flat *_prev fields over the legacy previous object", () => {
    const p = previousStats({ ...base, datasets_prev: 8, avg_quality_prev: 0.5, previous: { datasets: 1, pipelines_active: 1 } });
    expect(p).toEqual({ datasets: 8, rows_total: null, avg_quality: 0.5, pipelines_active: 1 });
    expect(pctChange(base.datasets, p!.datasets)).toBe(25);
  });
});
