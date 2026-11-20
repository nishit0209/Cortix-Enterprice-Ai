/**
 * Pipeline step catalog for the visual builder (node types per the W2 data_pipelines brief).
 * Each entry declares its category, arity (inputs/outputs) and the config fields the inspector renders.
 */

export type StepCategory = "source" | "schema" | "clean" | "reshape" | "destination";

export type FieldKind =
  | "dataset"
  | "column"
  | "columns"
  | "text"
  | "number"
  | "select"
  | "rename_map"
  | "casts"
  | "conditions"
  | "aggregations"
  | "sorts"
  | "join_keys"
  | "expression";

export interface FieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  required?: boolean;
  options?: Array<{ value: string; label: string }>;
  hint?: string;
  placeholder?: string;
  /** Only shown when another field has one of these values. */
  showIf?: { key: string; in: string[] };
}

export interface StepSpec {
  type: string;
  label: string;
  category: StepCategory;
  description: string;
  inputs: number;
  outputs: number;
  fields: FieldSpec[];
  defaults: Record<string, unknown>;
}

export const CAST_TYPES = ["text", "bigint", "double precision", "numeric", "boolean", "date", "timestamptz"].map((v) => ({ value: v, label: v }));
export const FILTER_OPS = [
  { value: "=", label: "equals" },
  { value: "!=", label: "not equals" },
  { value: ">", label: ">" },
  { value: ">=", label: "≥" },
  { value: "<", label: "<" },
  { value: "<=", label: "≤" },
  { value: "in", label: "in list" },
  { value: "not_in", label: "not in list" },
  { value: "contains", label: "contains" },
  { value: "is_null", label: "is empty" },
  { value: "not_null", label: "is not empty" },
];
export const AGG_FUNCS = ["sum", "avg", "min", "max", "count", "count_distinct", "median"].map((v) => ({ value: v, label: v }));

export const STEP_CATALOG: StepSpec[] = [
  {
    type: "source",
    label: "Dataset source",
    category: "source",
    description: "Read the current version of a dataset.",
    inputs: 0,
    outputs: 1,
    fields: [{ key: "dataset_id", label: "Dataset", kind: "dataset", required: true }],
    defaults: { dataset_id: "" },
  },
  {
    type: "select_columns",
    label: "Select columns",
    category: "schema",
    description: "Keep only the chosen columns.",
    inputs: 1,
    outputs: 1,
    fields: [{ key: "columns", label: "Columns to keep", kind: "columns", required: true }],
    defaults: { columns: [] },
  },
  {
    type: "rename",
    label: "Rename",
    category: "schema",
    description: "Rename columns.",
    inputs: 1,
    outputs: 1,
    fields: [{ key: "mapping", label: "Renames", kind: "rename_map", required: true }],
    defaults: { mapping: {} },
  },
  {
    type: "cast",
    label: "Cast types",
    category: "schema",
    description: "Change column types; failed casts become null or reject the row.",
    inputs: 1,
    outputs: 1,
    fields: [
      { key: "casts", label: "Casts", kind: "casts", required: true },
      { key: "on_error", label: "On a failed cast", kind: "select", options: [{ value: "null", label: "Set null" }, { value: "reject", label: "Reject row" }] },
    ],
    defaults: { casts: [], on_error: "null" },
  },
  {
    type: "trim_whitespace",
    label: "Trim whitespace",
    category: "clean",
    description: "Strip leading/trailing spaces (all text columns when none chosen).",
    inputs: 1,
    outputs: 1,
    fields: [{ key: "columns", label: "Columns", kind: "columns", hint: "Leave empty for every text column." }],
    defaults: { columns: [] },
  },
  {
    type: "change_case",
    label: "Change case",
    category: "clean",
    description: "Lower, upper or title-case text.",
    inputs: 1,
    outputs: 1,
    fields: [
      { key: "columns", label: "Columns", kind: "columns", required: true },
      { key: "case", label: "Case", kind: "select", required: true, options: [{ value: "lower", label: "lowercase" }, { value: "upper", label: "UPPERCASE" }, { value: "title", label: "Title Case" }] },
    ],
    defaults: { columns: [], case: "lower" },
  },
  {
    type: "fill_null",
    label: "Fill nulls",
    category: "clean",
    description: "Replace missing values with a constant or a statistic.",
    inputs: 1,
    outputs: 1,
    fields: [
      { key: "columns", label: "Columns", kind: "columns", required: true },
      {
        key: "strategy",
        label: "Strategy",
        kind: "select",
        required: true,
        options: ["constant", "mean", "median", "mode", "forward_fill"].map((v) => ({ value: v, label: v.replace("_", " ") })),
      },
      { key: "value", label: "Value", kind: "text", required: true, showIf: { key: "strategy", in: ["constant"] } },
    ],
    defaults: { columns: [], strategy: "constant", value: "" },
  },
  {
    type: "drop_nulls",
    label: "Drop null rows",
    category: "clean",
    description: "Remove rows with nulls in the chosen columns (any column when none chosen).",
    inputs: 1,
    outputs: 1,
    fields: [{ key: "columns", label: "Columns", kind: "columns", hint: "Leave empty to check every column." }],
    defaults: { columns: [] },
  },
  {
    type: "deduplicate",
    label: "Deduplicate",
    category: "clean",
    description: "Remove duplicate rows by key columns (whole row when none chosen).",
    inputs: 1,
    outputs: 1,
    fields: [
      { key: "keys", label: "Key columns", kind: "columns", hint: "Leave empty to compare whole rows." },
      { key: "keep", label: "Keep", kind: "select", options: [{ value: "first", label: "First" }, { value: "last", label: "Last" }] },
    ],
    defaults: { keys: [], keep: "first" },
  },
  {
    type: "filter",
    label: "Filter rows",
    category: "reshape",
    description: "Keep rows matching structured conditions (no raw SQL).",
    inputs: 1,
    outputs: 1,
    fields: [
      { key: "conditions", label: "Conditions", kind: "conditions", required: true },
      { key: "mode", label: "Match", kind: "select", options: [{ value: "all", label: "All conditions" }, { value: "any", label: "Any condition" }] },
    ],
    defaults: { conditions: [], mode: "all" },
  },
  {
    type: "derive",
    label: "Derive column",
    category: "reshape",
    description: "Add a column from a safe expression (validated server-side).",
    inputs: 1,
    outputs: 1,
    fields: [
      { key: "name", label: "New column", kind: "text", required: true, placeholder: "net_amount" },
      { key: "expression", label: "Expression", kind: "expression", required: true, placeholder: "amount - discount" },
    ],
    defaults: { name: "", expression: "" },
  },
  {
    type: "sort",
    label: "Sort",
    category: "reshape",
    description: "Order rows by one or more columns.",
    inputs: 1,
    outputs: 1,
    fields: [{ key: "by", label: "Sort by", kind: "sorts", required: true }],
    defaults: { by: [] },
  },
  {
    type: "aggregate",
    label: "Aggregate",
    category: "reshape",
    description: "Group rows and compute aggregates.",
    inputs: 1,
    outputs: 1,
    fields: [
      { key: "group_by", label: "Group by", kind: "columns" },
      { key: "aggregations", label: "Aggregations", kind: "aggregations", required: true },
    ],
    defaults: { group_by: [], aggregations: [] },
  },
  {
    type: "join",
    label: "Join",
    category: "reshape",
    description: "Join two inputs on key columns (left input = top handle).",
    inputs: 2,
    outputs: 1,
    fields: [
      { key: "how", label: "Join type", kind: "select", required: true, options: [{ value: "inner", label: "Inner" }, { value: "left", label: "Left" }] },
      { key: "on", label: "Join keys", kind: "join_keys", required: true },
    ],
    defaults: { how: "inner", on: [] },
  },
  {
    type: "destination",
    label: "Dataset output",
    category: "destination",
    description: "Write the result as a new dataset version.",
    inputs: 1,
    outputs: 0,
    fields: [
      { key: "dataset_name", label: "Output dataset name", kind: "text", required: true, placeholder: "sales_clean" },
      { key: "mode", label: "Write mode", kind: "select", options: [{ value: "new_version", label: "New version (replace)" }, { value: "append", label: "Append (new version with appended rows)" }] },
    ],
    defaults: { dataset_name: "", mode: "new_version" },
  },
];

export const STEP_BY_TYPE: Record<string, StepSpec> = Object.fromEntries(STEP_CATALOG.map((s) => [s.type, s]));

/** Type names other backends/docs use, mapped to the catalog (DATA_ENGINEERING_DESIGN §5.2 dotted names). */
const TYPE_ALIASES: Record<string, string> = {
  "source.dataset": "source",
  source_dataset: "source",
  dataset_source: "source",
  "destination.dataset": "destination",
  destination_dataset: "destination",
  dataset_destination: "destination",
  "transform.select": "select_columns",
  select: "select_columns",
  "transform.rename": "rename",
  "transform.cast": "cast",
  "transform.filter": "filter",
  "transform.derive": "derive",
  "transform.sort": "sort",
  "transform.aggregate": "aggregate",
  "transform.join": "join",
  "clean.trim_whitespace": "trim_whitespace",
  "clean.case": "change_case",
  "clean.fill_null": "fill_null",
  "clean.drop_null_rows": "drop_nulls",
  drop_null_rows: "drop_nulls",
  "dedup.exact": "deduplicate",
  dedup: "deduplicate",
};

export function canonicalType(type: string): string {
  return STEP_BY_TYPE[type] ? type : (TYPE_ALIASES[type] ?? type);
}

export const CATEGORY_META: Record<StepCategory, { label: string; color: string }> = {
  source: { label: "Sources", color: "var(--chart-2)" },
  schema: { label: "Schema", color: "var(--chart-5)" },
  clean: { label: "Clean", color: "var(--chart-3)" },
  reshape: { label: "Reshape", color: "var(--chart-7)" },
  destination: { label: "Destination", color: "var(--chart-1)" },
};
