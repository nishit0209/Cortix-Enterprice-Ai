import { describe, expect, it } from "vitest";
import { coerceFeature, curlExample, initialFormValues, inputTypeFor, serverFieldErrors, validatePredictionForm } from "./predict-form";
import type { FeatureSpec } from "./types";

const f = (name: string, dtype: FeatureSpec["dtype"], extra: Partial<FeatureSpec> = {}): FeatureSpec => ({
  name,
  dtype,
  categories: [],
  required: false,
  min: null,
  max: null,
  ...extra,
});

const SCHEMA: FeatureSpec[] = [
  f("tenure", "integer", { min: 0, max: 600 }),
  f("spend", "number"),
  f("region", "category", { categories: ["North", "South"] }),
  f("vip", "boolean"),
  f("signup", "datetime"),
  f("notes", "text", { required: true }),
];

describe("schema-driven prediction form", () => {
  it("starts empty for every feature", () => {
    expect(initialFormValues(SCHEMA)).toEqual({ tenure: "", spend: "", region: "", vip: "", signup: "", notes: "" });
  });

  it("coerces valid values and sends empty optional fields as null (imputed by the model)", () => {
    const r = validatePredictionForm(SCHEMA, { tenure: "12", spend: "1,234.5", region: "East", vip: "yes", signup: "2026-03-01", notes: " hi " });
    expect(r.errors).toEqual({});
    expect(r.row).toEqual({ tenure: 12, spend: 1234.5, region: "East", vip: true, signup: "2026-03-01", notes: "hi" });
    const sparse = validatePredictionForm(SCHEMA, { notes: "x" });
    expect(sparse.row).toEqual({ tenure: null, spend: null, region: null, vip: null, signup: null, notes: "x" });
    expect(Object.keys(sparse.row!)).toHaveLength(SCHEMA.length);
  });

  it("reports per-field errors and returns no row", () => {
    const r = validatePredictionForm(SCHEMA, { tenure: "1.5", spend: "abc", vip: "maybe", signup: "not a date", notes: "" });
    expect(r.row).toBeNull();
    expect(r.errors).toEqual({
      tenure: "Enter a whole number.",
      spend: "Enter a number.",
      vip: "Choose true or false.",
      signup: "Enter a valid date.",
      notes: "Required.",
    });
    expect(coerceFeature(SCHEMA[0], "-1").error).toBe("Must be at least 0.");
    expect(coerceFeature(SCHEMA[0], "601").error).toBe("Must be at most 600.");
    expect(coerceFeature(SCHEMA[3], "0").value).toBe(false);
    expect(coerceFeature(SCHEMA[4], "2026-03-01T10:00:00Z").value).toBe("2026-03-01T10:00:00.000Z");
  });

  it("chooses input types and maps server 400 field errors onto features", () => {
    expect(SCHEMA.map(inputTypeFor)).toEqual(["number", "number", "text", "text", "date", "text"]);
    expect(
      serverFieldErrors(
        [
          { field: "rows.0.tenure", message: "Missing column." },
          { field: "rows[0].spend", message: "Not a number." },
          { field: "unknown", message: "x" },
          { field: null, message: "y" },
        ],
        SCHEMA,
      ),
    ).toEqual({ tenure: "Missing column.", spend: "Not a number." });
  });

  it("builds a curl example with a placeholder secret only", () => {
    const text = curlExample({ origin: "http://localhost:3000", org: "acme", modelId: "m1", keyPrefix: "ab12cd", schema: SCHEMA });
    expect(text).toContain("/api/v1/orgs/acme/ml/models/m1/predict");
    expect(text).toContain("Bearer nxk_ab12cd_<secret>");
    expect(text).toContain('"region": "North"');
    expect(text).toContain('"explain": true');
    expect(curlExample({ origin: "", org: "a", modelId: "m", schema: [] })).toContain("nxk_<prefix>_<secret>");
  });
});
