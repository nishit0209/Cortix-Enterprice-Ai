import { describe, expect, it } from "vitest";
import { buildDefinition, compareRecords, describeDefinition, explanationOf, featureScores, statEntries } from "./mdm";

describe("compareRecords", () => {
  it("unions keys, orders by priority and flags differences", () => {
    const rows = compareRecords({ name: "Asha Rao", email: "asha@x.in", city: "Pune" }, { name: "asha  rao", email: "a.rao@y.in", phone: "98" }, ["email", "name"]);
    expect(rows.map((r) => r.key)).toEqual(["email", "name", "city", "phone"]);
    expect(rows.find((r) => r.key === "name")?.state).toBe("same");
    expect(rows.find((r) => r.key === "email")?.state).toBe("different");
    expect(rows.find((r) => r.key === "city")?.state).toBe("one");
  });

  it("drops keys empty on both sides and renders arrays", () => {
    const rows = compareRecords({ a: "", tags: ["x", "y"] }, { a: null, tags: ["x"] });
    expect(rows).toEqual([{ key: "tags", a: "x, y", b: "x", state: "different" }]);
  });
});

describe("featureScores / statEntries", () => {
  it("keeps finite numeric features, highest first", () => {
    expect(featureScores({ email: 1, name: 0.82, phone: null, note: "x", nan: Number.NaN })).toEqual([
      { name: "email", value: 1 },
      { name: "name", value: 0.82 },
    ]);
    expect(featureScores(null)).toEqual([]);
  });

  it("flattens one level of nested run stats", () => {
    const out = statEntries({ profiles: 120, merged: 4, sources: { crm: { records: 10 }, web: 5 }, long: "x".repeat(80) });
    expect(out).toEqual([
      { label: "profiles", value: 120 },
      { label: "merged", value: 4 },
      { label: "sources · web", value: 5 },
    ]);
  });
});

describe("rule segment definitions", () => {
  it("builds typed conditions and list values", () => {
    const { definition, errors } = buildDefinition("any", [
      { field: "order_count", op: "gte", value: "3" },
      { field: "rfm_label", op: "in", value: "Champions, At risk" },
      { field: "golden.email", op: "not_null", value: "" },
    ]);
    expect(errors).toEqual({});
    expect(definition).toEqual({
      match: "any",
      conditions: [
        { field: "order_count", op: "gte", value: 3 },
        { field: "rfm_label", op: "in", value: ["Champions", "At risk"] },
        { field: "golden.email", op: "not_null", value: null },
      ],
    });
  });

  it("reports per-row errors and rejects empty definitions", () => {
    expect(buildDefinition("all", []).errors[-1]).toMatch(/at least one/);
    const { definition, errors } = buildDefinition("all", [
      { field: "monetary", op: "gt", value: "lots" },
      { field: "unknown", op: "eq", value: "1" },
      { field: "monetary", op: "eq", value: "" },
    ]);
    expect(definition).toBeNull();
    expect(errors).toEqual({ 0: "Enter a number.", 1: "Pick a field.", 2: "Enter a value." });
  });

  it("describes stored rule and RFM definitions", () => {
    expect(describeDefinition({ match: "all", conditions: [{ field: "order_count", op: "gte", value: 3 }, { field: "golden.email", op: "is_null" }] })).toBe("Orders ≥ 3 and Email is empty");
    expect(describeDefinition({ label: "Champions" })).toBe('RFM segment "Champions"');
  });
});

describe("explanationOf", () => {
  it("reads contribution lists and sorts by absolute size", () => {
    const ex = explanationOf({ text: "High risk", contributions: [{ feature: "tenure_days", contribution: -0.1 }, { feature: "recency_days", contribution: 0.4, value: 120 }] });
    expect(ex.text).toBe("High risk");
    expect(ex.contributions).toEqual([
      { feature: "recency_days", contribution: 0.4, value: "120" },
      { feature: "tenure_days", contribution: -0.1 },
    ]);
  });

  it("accepts feature maps, reason lists and plain strings", () => {
    expect(explanationOf({ shap: { a: 0.1, b: -0.3 } }).contributions.map((c) => c.feature)).toEqual(["b", "a"]);
    expect(explanationOf({ reasons: ["No order in 90 days"] }).reasons).toEqual(["No order in 90 days"]);
    expect(explanationOf("Rule: champions").text).toBe("Rule: champions");
    expect(explanationOf(null)).toEqual({ text: null, reasons: [], contributions: [] });
  });
});
