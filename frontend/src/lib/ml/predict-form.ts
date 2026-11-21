/**
 * Prediction playground form, generated from a model version's feature schema.
 *
 * Every feature is sent (the API rejects missing columns with a 400 listing them); an empty optional input is
 * sent as `null` so the model's own imputer handles it. Unknown categories are allowed (the pipeline one-hot
 * encoder ignores them), so a category input is a combobox, not a closed select. Unit-tested.
 */

import type { FeatureSpec } from "./types";

export type FormValues = Record<string, string>;

export function initialFormValues(schema: FeatureSpec[]): FormValues {
  return Object.fromEntries(schema.map((f) => [f.name, ""]));
}

export interface FormResult {
  row: Record<string, unknown> | null;
  errors: Record<string, string>;
}

export function coerceFeature(f: FeatureSpec, raw: string | undefined): { value: unknown; error?: string } {
  const text = (raw ?? "").trim();
  if (text === "") return f.required ? { value: null, error: "Required." } : { value: null };
  switch (f.dtype) {
    case "number":
    case "integer": {
      const n = Number(text.replace(/,/g, ""));
      if (!Number.isFinite(n)) return { value: null, error: "Enter a number." };
      if (f.dtype === "integer" && !Number.isInteger(n)) return { value: null, error: "Enter a whole number." };
      if (f.min !== null && n < f.min) return { value: null, error: `Must be at least ${f.min}.` };
      if (f.max !== null && n > f.max) return { value: null, error: `Must be at most ${f.max}.` };
      return { value: n };
    }
    case "boolean": {
      const t = text.toLowerCase();
      if (["true", "yes", "1"].includes(t)) return { value: true };
      if (["false", "no", "0"].includes(t)) return { value: false };
      return { value: null, error: "Choose true or false." };
    }
    case "datetime": {
      const ms = Date.parse(text);
      if (Number.isNaN(ms)) return { value: null, error: "Enter a valid date." };
      return { value: /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : new Date(ms).toISOString() };
    }
    default:
      if (text.length > 1000) return { value: null, error: "At most 1,000 characters." };
      return { value: text };
  }
}

/** Validate the whole form; `row` is the JSON record to POST, or null when any field is invalid. */
export function validatePredictionForm(schema: FeatureSpec[], values: FormValues): FormResult {
  const row: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of schema) {
    const r = coerceFeature(f, values[f.name]);
    if (r.error) errors[f.name] = r.error;
    else row[f.name] = r.value;
  }
  return { row: Object.keys(errors).length ? null : row, errors };
}

/** HTML input type for a feature. */
export function inputTypeFor(f: FeatureSpec): "number" | "text" | "date" {
  if (f.dtype === "number" || f.dtype === "integer") return "number";
  if (f.dtype === "datetime") return "date";
  return "text";
}

/** Field errors from a 400 problem response (`errors: [{field, message}]`) mapped onto feature names. */
export function serverFieldErrors(errors: Array<{ field?: string | null; message: string }>, schema: FeatureSpec[]): Record<string, string> {
  const names = new Set(schema.map((f) => f.name));
  const out: Record<string, string> = {};
  for (const e of errors) {
    const field = (e.field ?? "").replace(/^rows\.\d+\./, "").replace(/^rows\[\d+\]\./, "");
    if (names.has(field) && !out[field]) out[field] = e.message;
  }
  return out;
}

/** A shell-safe curl example for the prediction API. The secret is always a placeholder. */
export function curlExample(opts: { origin: string; org: string; modelId: string; keyPrefix?: string | null; schema: FeatureSpec[] }): string {
  const example: Record<string, unknown> = {};
  for (const f of opts.schema.slice(0, 12)) {
    example[f.name] =
      f.dtype === "number" || f.dtype === "integer"
        ? (f.min ?? 0)
        : f.dtype === "boolean"
          ? false
          : f.dtype === "category" && f.categories.length
            ? f.categories[0]
            : f.dtype === "datetime"
              ? "2026-01-31"
              : "…";
  }
  const token = opts.keyPrefix ? `nxk_${opts.keyPrefix}_<secret>` : "nxk_<prefix>_<secret>";
  const body = JSON.stringify({ rows: [example], explain: true }, null, 2).replace(/'/g, "'\\''");
  return [
    `curl -X POST '${opts.origin}/api/v1/orgs/${opts.org}/ml/models/${opts.modelId}/predict' \\`,
    `  -H 'Authorization: Bearer ${token}' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d '${body}'`,
  ].join("\n");
}
