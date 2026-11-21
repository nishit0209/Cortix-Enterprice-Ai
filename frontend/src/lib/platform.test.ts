import { describe, expect, it } from "vitest";
import type { MeResponse } from "@/lib/types";
import { COMPONENT_RE, healthTone, logTone, orgStatusTone, parseQuota, platformGate, usageByDayAndModel } from "./platform";

const me = (admin: boolean) => ({ user: { id: "u", email: "a@b.c", full_name: "A", is_platform_admin: admin }, memberships: [] }) as unknown as MeResponse;

describe("platformGate", () => {
  it("waits while /me loads", () => expect(platformGate(undefined, true)).toBe("loading"));
  it("hides the console when /me failed to load", () => expect(platformGate(undefined, false)).toBe("not_found"));
  it("sends signed-out visitors to login", () => expect(platformGate(null, false)).toBe("signed_out"));
  it("renders 404 to non-admins", () => expect(platformGate(me(false), false)).toBe("not_found"));
  it("allows platform admins", () => expect(platformGate(me(true), false)).toBe("allowed"));
  it("does not trust truthy non-boolean flags", () => {
    const m = me(false);
    (m.user as unknown as Record<string, unknown>).is_platform_admin = "true";
    expect(platformGate(m, false)).toBe("not_found");
  });
});

describe("tones", () => {
  it("maps health statuses", () => {
    expect(healthTone("ok")).toBe("success");
    expect(healthTone("degraded")).toBe("warning");
    expect(healthTone("unavailable")).toBe("warning");
    expect(healthTone("error")).toBe("danger");
    expect(healthTone(undefined)).toBe("neutral");
  });
  it("maps org statuses and log levels", () => {
    expect(orgStatusTone("active")).toBe("success");
    expect(orgStatusTone("suspended")).toBe("warning");
    expect(orgStatusTone("deleted")).toBe("danger");
    expect(logTone("CRITICAL")).toBe("danger");
    expect(logTone("WARNING")).toBe("warning");
    expect(logTone("INFO")).toBe("info");
  });
});

describe("COMPONENT_RE", () => {
  it("mirrors the server filter", () => {
    expect(COMPONENT_RE.test("apps.ml.tasks")).toBe(true);
    expect(COMPONENT_RE.test("bad component")).toBe(false);
    expect(COMPONENT_RE.test("x".repeat(121))).toBe(false);
  });
});

describe("parseQuota", () => {
  it("returns a body for valid input (separators allowed)", () => {
    const r = parseQuota({ rpm: "15", tpm: "1,000,000", rpd: "1_500", notes: "  free tier " });
    expect(r.errors).toEqual({});
    expect(r.body).toEqual({ rpm: 15, tpm: 1_000_000, rpd: 1500, notes: "free tier", verified: true });
  });
  it("rejects empty, non-integer and out-of-range values", () => {
    const r = parseQuota({ rpm: "", tpm: "1.5", rpd: "100000001", notes: "" });
    expect(r.body).toBeNull();
    expect(r.errors.rpm).toBe("Required.");
    expect(r.errors.tpm).toBe("Whole number.");
    expect(r.errors.rpd).toMatch(/^1 to /);
  });
  it("rejects zero and long notes", () => {
    const r = parseQuota({ rpm: "0", tpm: "1", rpd: "1", notes: "x".repeat(201) });
    expect(r.errors.rpm).toMatch(/^1 to /);
    expect(r.errors.notes).toBeDefined();
  });
});

describe("usageByDayAndModel", () => {
  it("pivots per day with zero-filled models, sorted by day", () => {
    const r = usageByDayAndModel([
      { day: "2026-11-02", model: "gemini-b", requests: 3 },
      { day: "2026-11-01", model: "gemini-a", requests: 2 },
      { day: "2026-11-02", model: "gemini-a", requests: 1 },
      { day: "2026-11-02", model: "gemini-a", requests: 4 },
    ]);
    expect(r.models).toEqual(["gemini-a", "gemini-b"]);
    expect(r.rows).toEqual([
      { day: "2026-11-01", "gemini-a": 2, "gemini-b": 0 },
      { day: "2026-11-02", "gemini-a": 5, "gemini-b": 3 },
    ]);
  });
  it("handles no data", () => expect(usageByDayAndModel([])).toEqual({ rows: [], models: [] }));
});
