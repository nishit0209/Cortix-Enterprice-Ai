/**
 * Row-policy condition builder model (never free SQL).
 *
 * The backend predicate (`apps/governance/services.validate_predicate`) is `{"all": [{col, op, value}]}`: ANDed
 * conditions, ops `= != < <= > >= in not_in is_null not_null`, a value that is a scalar, a list (for in/not_in),
 * absent (for the null ops) or a `$user.<attribute>` reference. These pure helpers convert between that wire
 * shape and the editable rows of the builder, validate with the backend's rules, and render a summary.
 */

export type Op = "=" | "!=" | "<" | "<=" | ">" | ">=" | "in" | "not_in" | "is_null" | "not_null";
export const OPS: Array<{ value: Op; label: string }> = [
  { value: "=", label: "equals" },
  { value: "!=", label: "does not equal" },
  { value: "<", label: "is less than" },
  { value: "<=", label: "is at most" },
  { value: ">", label: "is greater than" },
  { value: ">=", label: "is at least" },
  { value: "in", label: "is one of" },
  { value: "not_in", label: "is not one of" },
  { value: "is_null", label: "is empty" },
  { value: "not_null", label: "is not empty" },
];
export const LIST_OPS = new Set<Op>(["in", "not_in"]);
export const NULL_OPS = new Set<Op>(["is_null", "not_null"]);
export const MAX_CONDITIONS = 20;
export const MAX_LIST_VALUES = 1000;
export const ATTRIBUTE_RE = /^[a-z_][a-z0-9_]{0,63}$/;
/** Built-in `$user.*` attributes every member has (backend BUILTIN_ATTRS). */
export const BUILTIN_ATTRIBUTES = ["id", "email", "role"];

export type ValueSource = "literal" | "user_attribute";

export interface ConditionRow {
  column: string;
  op: Op;
  source: ValueSource;
  /** Literal scalar (text as typed). */
  value: string;
  /** Literal list for in/not_in. */
  list: string[];
  /** Attribute key for `$user.<key>`. */
  attribute: string;
}

export interface WireCondition {
  col: string;
  op: Op;
  value?: unknown;
}

export interface WirePredicate {
  all: WireCondition[];
}

export function emptyCondition(column = ""): ConditionRow {
  return { column, op: "=", source: "literal", value: "", list: [], attribute: "" };
}

const isNumericType = (dtype: string | undefined) => !!dtype && /int|float|double|decimal|numeric|number/i.test(dtype);
const isBoolType = (dtype: string | undefined) => !!dtype && /bool/i.test(dtype);

/** Coerce a typed literal by the column's type (numbers/booleans stay typed on the wire). */
export function coerceLiteral(text: string, dtype?: string): string | number | boolean {
  const t = text.trim();
  if (isNumericType(dtype) && t !== "" && Number.isFinite(Number(t))) return Number(t);
  if (isBoolType(dtype) && /^(true|false)$/i.test(t)) return t.toLowerCase() === "true";
  return t;
}

/** Validation errors per row index (empty object = valid). Mirrors the backend rules. */
export function validateConditions(rows: ConditionRow[], columns?: string[]): Record<number, string> {
  const errors: Record<number, string> = {};
  if (rows.length === 0) errors[-1] = "Add at least one condition.";
  if (rows.length > MAX_CONDITIONS) errors[-1] = `At most ${MAX_CONDITIONS} conditions.`;
  rows.forEach((r, i) => {
    if (!r.column.trim()) errors[i] = "Choose a column.";
    else if (columns && !columns.includes(r.column)) errors[i] = `Unknown column "${r.column}".`;
    else if (/[\x00-\x1f"]/.test(r.column) || r.column.length > 128) errors[i] = "Invalid column name.";
    else if (NULL_OPS.has(r.op)) return;
    else if (r.source === "user_attribute") {
      if (!ATTRIBUTE_RE.test(r.attribute)) errors[i] = "Attribute keys are lowercase snake_case.";
    } else if (LIST_OPS.has(r.op)) {
      const vals = r.list.map((v) => v.trim()).filter(Boolean);
      if (vals.length === 0) errors[i] = "Add at least one value.";
      else if (vals.length > MAX_LIST_VALUES) errors[i] = `At most ${MAX_LIST_VALUES} values.`;
      else if (vals.some((v) => v.startsWith("$user."))) errors[i] = "Use the user-attribute source instead of $user inside a list.";
    } else if (r.value.trim() === "") errors[i] = "Enter a value.";
    else if (r.value.trim().startsWith("$user.")) errors[i] = "Use the user-attribute source for $user values.";
  });
  return errors;
}

/** Builder rows -> wire predicate. `types` maps column -> data type for literal coercion. */
export function serializeConditions(rows: ConditionRow[], types: Record<string, string> = {}): WirePredicate {
  return {
    all: rows.map((r): WireCondition => {
      const col = r.column.trim();
      if (NULL_OPS.has(r.op)) return { col, op: r.op };
      if (r.source === "user_attribute") return { col, op: r.op, value: `$user.${r.attribute.trim()}` };
      if (LIST_OPS.has(r.op)) {
        const vals = Array.from(new Set(r.list.map((v) => v.trim()).filter(Boolean)));
        return { col, op: r.op, value: vals.map((v) => coerceLiteral(v, types[col])) };
      }
      return { col, op: r.op, value: coerceLiteral(r.value, types[col]) };
    }),
  };
}

/** Wire predicate -> builder rows (for editing an existing policy). Unknown shapes yield no rows. */
export function parsePredicate(predicate: unknown): ConditionRow[] {
  if (!predicate || typeof predicate !== "object" || !Array.isArray((predicate as WirePredicate).all)) return [];
  return (predicate as WirePredicate).all
    .filter((c) => c && typeof c.col === "string" && OPS.some((o) => o.value === c.op))
    .map((c) => {
      const row = emptyCondition(c.col);
      row.op = c.op;
      if (NULL_OPS.has(c.op)) return row;
      if (typeof c.value === "string" && c.value.startsWith("$user.")) {
        row.source = "user_attribute";
        row.attribute = c.value.slice(6);
      } else if (Array.isArray(c.value)) row.list = c.value.map((v) => String(v));
      else row.value = c.value === null || c.value === undefined ? "" : String(c.value);
      return row;
    });
}

function fmtValue(v: unknown): string {
  if (typeof v === "string") return v.startsWith("$user.") ? v : `'${v}'`;
  return String(v);
}

/** One-line human summary of a predicate: `region is one of $user.regions AND amount > 100`. */
export function summarizePredicate(predicate: WirePredicate | WireCondition[] | null | undefined): string {
  const conds = Array.isArray(predicate) ? predicate : (predicate?.all ?? []);
  if (!conds.length) return "All rows";
  return conds
    .map((c) => {
      const label = OPS.find((o) => o.value === c.op)?.label ?? c.op;
      if (NULL_OPS.has(c.op)) return `${c.col} ${label}`;
      if (Array.isArray(c.value)) {
        const shown = c.value.slice(0, 5).map(fmtValue).join(", ");
        return `${c.col} ${label} (${shown}${c.value.length > 5 ? `, +${c.value.length - 5} more` : ""})`;
      }
      return `${c.col} ${label} ${fmtValue(c.value)}`;
    })
    .join(" AND ");
}

/** Parse a comma/newline separated list typed into the builder. */
export function splitList(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((v) => v.trim())
    .filter(Boolean);
}
