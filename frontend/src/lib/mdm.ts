/**
 * Pure helpers shared by Customer 360 and Product 360 (master-data screens): side-by-side record comparison,
 * run statistics and rule-segment conditions. Unit-tested in `mdm.test.ts`.
 */

export type Scalar = string | number | boolean | null;

export interface CompareRow {
  key: string;
  a: string;
  b: string;
  /** "same" (equal after case/space folding), "different", or "one" (only one side has a value). */
  state: "same" | "different" | "one";
}

export function displayValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return "";
  if (Array.isArray(v)) return v.map(displayValue).filter(Boolean).join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

const fold = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim();

/** Union of the attribute keys of two records, ordered by `priority` first, then alphabetically. */
export function compareRecords(a: Record<string, unknown> | undefined, b: Record<string, unknown> | undefined, priority: string[] = []): CompareRow[] {
  const keys = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]);
  const rank = (k: string) => {
    const i = priority.indexOf(k);
    return i === -1 ? priority.length : i;
  };
  return [...keys]
    .sort((x, y) => rank(x) - rank(y) || x.localeCompare(y))
    .map((key) => {
      const av = displayValue(a?.[key]);
      const bv = displayValue(b?.[key]);
      const state: CompareRow["state"] = !av || !bv ? "one" : fold(av) === fold(bv) ? "same" : "different";
      return { key, a: av, b: bv, state };
    })
    .filter((r) => r.a || r.b);
}

/** Numeric feature scores of a match (0..1), highest first; non-numeric entries are dropped. */
export function featureScores(features: unknown): Array<{ name: string; value: number }> {
  if (!features || typeof features !== "object" || Array.isArray(features)) return [];
  return Object.entries(features as Record<string, unknown>)
    .filter((e): e is [string, number] => typeof e[1] === "number" && Number.isFinite(e[1]))
    .map(([name, value]) => ({ name, value }))
    .sort((x, y) => y.value - x.value);
}

/** Flat `label: value` pairs of a run's stats (numbers and short strings only, nested objects one level deep). */
export function statEntries(stats: unknown, prefix = ""): Array<{ label: string; value: Scalar }> {
  if (!stats || typeof stats !== "object" || Array.isArray(stats)) return [];
  const out: Array<{ label: string; value: Scalar }> = [];
  for (const [k, v] of Object.entries(stats as Record<string, unknown>)) {
    const label = prefix ? `${prefix} · ${k}` : k;
    if (typeof v === "number" || typeof v === "boolean" || (typeof v === "string" && v.length <= 60)) out.push({ label: label.replace(/_/g, " "), value: v });
    else if (v && typeof v === "object" && !Array.isArray(v) && !prefix) out.push(...statEntries(v, k));
  }
  return out;
}

/* ---------------- score explanations ---------------- */

export interface Explanation {
  text: string | null;
  reasons: string[];
  contributions: Array<{ feature: string; contribution: number; value?: string }>;
}

const CONTRIB_KEYS = ["contributions", "top_contributions", "top_features", "shap", "features"];

/**
 * Normalizes a score explanation: a sentence, a list of reasons, and/or per-feature contributions. Accepts
 * `[{feature|name, contribution|shap|value}]`, `{feature: number}` maps, or an object holding one of those
 * under a known key. Contributions are ordered by absolute size.
 */
export function explanationOf(raw: unknown): Explanation {
  const out: Explanation = { text: null, reasons: [], contributions: [] };
  if (typeof raw === "string") return { ...out, text: raw };
  const fromList = (list: unknown[]) => {
    for (const item of list) {
      if (typeof item === "string") out.reasons.push(item);
      else if (item && typeof item === "object") {
        const o = item as Record<string, unknown>;
        const feature = o.feature ?? o.name ?? o.column;
        const c = [o.contribution, o.shap, o.impact, o.weight].find((x) => typeof x === "number" && Number.isFinite(x));
        if (typeof feature === "string" && typeof c === "number") {
          out.contributions.push({ feature, contribution: c, ...(o.value !== undefined && o.value !== null ? { value: displayValue(o.value) } : {}) });
        }
      }
    }
  };
  if (Array.isArray(raw)) fromList(raw);
  else if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    for (const k of ["text", "summary", "message", "reason"]) if (typeof o[k] === "string" && !out.text) out.text = o[k] as string;
    if (Array.isArray(o.reasons)) fromList(o.reasons);
    let found = false;
    for (const k of CONTRIB_KEYS) {
      const v = o[k];
      if (Array.isArray(v)) {
        fromList(v);
        found = true;
      } else if (v && typeof v === "object") {
        for (const [f, c] of Object.entries(v as Record<string, unknown>)) if (typeof c === "number" && Number.isFinite(c)) out.contributions.push({ feature: f, contribution: c });
        found = true;
      }
      if (found) break;
    }
    if (!found && !out.text && !out.reasons.length) {
      for (const [f, c] of Object.entries(o)) if (typeof c === "number" && Number.isFinite(c)) out.contributions.push({ feature: f, contribution: c });
    }
  }
  out.contributions.sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
  return out;
}

/* ---------------- rule segments ---------------- */

export interface SegmentField {
  value: string;
  label: string;
  kind: "number" | "text" | "date" | "list";
}

/** Mirrors `apps/customers/matching.py` SEGMENT_FIELDS + `golden.<field>`. */
export const SEGMENT_FIELDS: SegmentField[] = [
  { value: "order_count", label: "Orders", kind: "number" },
  { value: "monetary", label: "Total spend", kind: "number" },
  { value: "recency_days", label: "Days since last order", kind: "number" },
  { value: "event_count", label: "Events", kind: "number" },
  { value: "record_count", label: "Linked records", kind: "number" },
  { value: "r", label: "RFM recency score (1-5)", kind: "number" },
  { value: "f", label: "RFM frequency score (1-5)", kind: "number" },
  { value: "m", label: "RFM monetary score (1-5)", kind: "number" },
  { value: "rfm_label", label: "RFM segment", kind: "text" },
  { value: "sources", label: "Source systems", kind: "list" },
  { value: "first_seen", label: "First seen", kind: "date" },
  { value: "last_seen", label: "Last seen", kind: "date" },
  { value: "golden.name", label: "Name", kind: "text" },
  { value: "golden.email", label: "Email", kind: "text" },
  { value: "golden.phone", label: "Phone", kind: "text" },
  { value: "golden.address", label: "Address", kind: "text" },
];

export const SEGMENT_OPS: Array<{ value: string; label: string; needsValue: boolean; list?: boolean }> = [
  { value: "eq", label: "is", needsValue: true },
  { value: "ne", label: "is not", needsValue: true },
  { value: "gt", label: ">", needsValue: true },
  { value: "gte", label: "≥", needsValue: true },
  { value: "lt", label: "<", needsValue: true },
  { value: "lte", label: "≤", needsValue: true },
  { value: "contains", label: "contains", needsValue: true },
  { value: "in", label: "is one of", needsValue: true, list: true },
  { value: "not_in", label: "is none of", needsValue: true, list: true },
  { value: "is_null", label: "is empty", needsValue: false },
  { value: "not_null", label: "is not empty", needsValue: false },
];

export interface DraftCondition {
  field: string;
  op: string;
  value: string;
}

export interface SegmentDefinition {
  match: "all" | "any";
  conditions: Array<{ field: string; op: string; value: Scalar | Scalar[] }>;
}

function coerce(raw: string, kind: SegmentField["kind"]): Scalar {
  const t = raw.trim();
  if (kind === "number") {
    const n = Number(t);
    return t !== "" && Number.isFinite(n) ? n : t;
  }
  return t;
}

/** Builds the API definition from editor rows; returns an error message per invalid row (index → message). */
export function buildDefinition(match: "all" | "any", rows: DraftCondition[]): { definition: SegmentDefinition | null; errors: Record<number, string> } {
  const errors: Record<number, string> = {};
  const conditions: SegmentDefinition["conditions"] = [];
  if (rows.length === 0) return { definition: null, errors: { [-1]: "Add at least one condition." } };
  if (rows.length > 20) return { definition: null, errors: { [-1]: "At most 20 conditions." } };
  rows.forEach((row, i) => {
    const field = SEGMENT_FIELDS.find((f) => f.value === row.field);
    const op = SEGMENT_OPS.find((o) => o.value === row.op);
    if (!field) return void (errors[i] = "Pick a field.");
    if (!op) return void (errors[i] = "Pick an operator.");
    if (!op.needsValue) return void conditions.push({ field: field.value, op: op.value, value: null });
    if (!row.value.trim()) return void (errors[i] = "Enter a value.");
    if (op.list) {
      const items = row.value.split(",").map((v) => coerce(v, field.kind)).filter((v) => v !== "");
      if (!items.length) return void (errors[i] = "Enter comma-separated values.");
      return void conditions.push({ field: field.value, op: op.value, value: items });
    }
    const value = coerce(row.value, field.kind);
    if (field.kind === "number" && ["gt", "gte", "lt", "lte"].includes(op.value) && typeof value !== "number") {
      return void (errors[i] = "Enter a number.");
    }
    conditions.push({ field: field.value, op: op.value, value });
  });
  return Object.keys(errors).length ? { definition: null, errors } : { definition: { match, conditions }, errors };
}

/** Human-readable sentence of a stored definition (rule or RFM). */
export function describeDefinition(def: unknown): string {
  if (!def || typeof def !== "object") return "";
  const d = def as { match?: string; conditions?: Array<{ field?: string; op?: string; value?: unknown }>; label?: string };
  if (d.label) return `RFM segment "${d.label}"`;
  const conds = Array.isArray(d.conditions) ? d.conditions : [];
  const parts = conds.map((c) => {
    const f = SEGMENT_FIELDS.find((x) => x.value === c.field)?.label ?? c.field ?? "?";
    const op = SEGMENT_OPS.find((x) => x.value === c.op);
    if (op && !op.needsValue) return `${f} ${op.label}`;
    return `${f} ${op?.label ?? c.op ?? "?"} ${displayValue(c.value)}`;
  });
  return parts.join(d.match === "any" ? " or " : " and ");
}
