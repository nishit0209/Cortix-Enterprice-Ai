/**
 * Automation step catalog. The server registry (`apps.automation.registry`, CONTRACT "W5 additions") is the source
 * of truth: `mergeCatalog` overlays whatever `GET /automation/steps` returns on this list of the kinds the
 * CONTRACT requires, so labels/forms exist even when the registry omits them, and unknown server kinds still show.
 */

export type StepImpact = "low" | "high";
export type StepCategory = "control" | "data" | "customers" | "products" | "ml" | "documents" | "bi";

export interface JsonSchemaProp {
  type?: string | string[];
  title?: string;
  description?: string;
  enum?: unknown[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  items?: { type?: string; enum?: unknown[] };
  format?: string;
}

export interface StepSchema {
  type?: string;
  properties?: Record<string, JsonSchemaProp>;
  required?: string[];
}

export interface StepKind {
  kind: string;
  label: string;
  description: string;
  category: StepCategory;
  impact: StepImpact;
  schema: StepSchema;
  /** Registered on this server (false = only known from the CONTRACT). */
  registered: boolean;
}

const s = (properties: Record<string, JsonSchemaProp>, required: string[] = []): StepSchema => ({ type: "object", properties, required });

export const BUILTIN_STEPS: StepKind[] = [
  {
    kind: "condition",
    label: "Condition",
    description: "Branch on a value from the trigger payload or an earlier step's output.",
    category: "control",
    impact: "low",
    registered: false,
    schema: s(
      {
        left: { type: "string", title: "Value path", description: "e.g. trigger.row_count or steps.query_1.rows" },
        op: { type: "string", title: "Operator", enum: ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "exists"] },
        right: { type: "string", title: "Compare with" },
      },
      ["left", "op"],
    ),
  },
  {
    kind: "notify",
    label: "Notify",
    description: "Send an in-app notification.",
    category: "control",
    impact: "low",
    registered: false,
    schema: s({ title: { type: "string", title: "Title" }, message: { type: "string", title: "Message" }, permission: { type: "string", title: "Notify holders of permission", description: "Empty: the workflow owner" } }, ["title"]),
  },
  {
    kind: "approval",
    label: "Approval",
    description: "Pause the run until someone with the permission approves.",
    category: "control",
    impact: "low",
    registered: false,
    schema: s(
      {
        title: { type: "string", title: "Title" },
        approver_permission: { type: "string", title: "Approver permission", default: "automation.manage" },
        impact: { type: "string", title: "Impact", enum: ["low", "high"], default: "low", description: "High impact decisions need password re-authentication." },
      },
      ["title", "approver_permission"],
    ),
  },
  {
    kind: "wait",
    label: "Wait",
    description: "Pause for a number of seconds.",
    category: "control",
    impact: "low",
    registered: false,
    schema: s({ seconds: { type: "integer", title: "Seconds", minimum: 1, maximum: 86400, default: 60 } }, ["seconds"]),
  },
  {
    kind: "pipelines.run",
    label: "Run pipeline",
    description: "Run a data pipeline and wait for it to finish.",
    category: "data",
    impact: "low",
    registered: false,
    schema: s({ pipeline_id: { type: "string", title: "Pipeline", format: "uuid" } }, ["pipeline_id"]),
  },
  {
    kind: "bi.saved_query",
    label: "Saved query",
    description: "Run a saved query through the SQL guard under the actor's data policy.",
    category: "bi",
    impact: "low",
    registered: false,
    schema: s({ saved_query_id: { type: "string", title: "Saved query", format: "uuid" } }, ["saved_query_id"]),
  },
  {
    kind: "customers.resolve",
    label: "Resolve customers",
    description: "Run Customer 360 identity resolution.",
    category: "customers",
    impact: "low",
    registered: false,
    schema: s({}),
  },
  {
    kind: "customers.score",
    label: "Score customers",
    description: "Score churn or lifetime value with a promoted model.",
    category: "customers",
    impact: "low",
    registered: false,
    schema: s({ score_type: { type: "string", title: "Score", enum: ["churn", "clv"] }, model_id: { type: "string", title: "Model", format: "uuid" } }, ["score_type", "model_id"]),
  },
  {
    kind: "products.normalize",
    label: "Normalize products",
    description: "Run Product 360 normalization and matching.",
    category: "products",
    impact: "low",
    registered: false,
    schema: s({}),
  },
  {
    kind: "ml.train",
    label: "Train model",
    description: "Train an experiment in an ML project.",
    category: "ml",
    impact: "low",
    registered: false,
    schema: s({ project_id: { type: "string", title: "ML project", format: "uuid" }, algorithm: { type: "string", title: "Algorithm", description: "Empty: the top recommendation" } }, ["project_id"]),
  },
  {
    kind: "ml.batch_predict",
    label: "Batch predict",
    description: "Score a dataset with a model and publish the predictions as a dataset.",
    category: "ml",
    impact: "low",
    registered: false,
    schema: s(
      { model_id: { type: "string", title: "Model", format: "uuid" }, dataset_id: { type: "string", title: "Input dataset", format: "uuid" }, output_name: { type: "string", title: "Output dataset name" } },
      ["model_id", "dataset_id"],
    ),
  },
  {
    kind: "documents.process",
    label: "Process document",
    description: "Extract, chunk and embed a document.",
    category: "documents",
    impact: "low",
    registered: false,
    schema: s({ document_id: { type: "string", title: "Document", format: "uuid" } }, ["document_id"]),
  },
];

export const CATEGORY_LABELS: Record<StepCategory, string> = {
  control: "Control",
  data: "Data",
  bi: "BI",
  customers: "Customer 360",
  products: "Product 360",
  ml: "ML",
  documents: "Documents",
};

export const TRIGGER_EVENTS = [
  { value: "dataset.ingest_finished", label: "A dataset finished ingesting" },
  { value: "pipeline.run_succeeded", label: "A pipeline run succeeded" },
  { value: "pipeline.run_failed", label: "A pipeline run failed" },
  { value: "document.processed", label: "A document was processed" },
];

function categoryOf(kind: string): StepCategory {
  const prefix = kind.split(".")[0];
  if (prefix === "pipelines" || prefix === "datasets") return "data";
  if (["bi", "customers", "products", "ml", "documents"].includes(prefix)) return prefix as StepCategory;
  return "control";
}

function titleOf(kind: string): string {
  const last = kind.split(".").pop() ?? kind;
  return last.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Overlays the server registry on the CONTRACT list. Server entries may be `{kind, schema, impact, label?, description?}`. */
export function mergeCatalog(remote: unknown): StepKind[] {
  const list = Array.isArray(remote) ? remote : remote && typeof remote === "object" && Array.isArray((remote as { items?: unknown }).items) ? (remote as { items: unknown[] }).items : null;
  if (!list) return BUILTIN_STEPS;
  const byKind = new Map(BUILTIN_STEPS.map((k) => [k.kind, { ...k }]));
  const registered = new Set<string>();
  for (const raw of list) {
    if (!raw || typeof raw !== "object") continue;
    const r = raw as Record<string, unknown>;
    const kind = typeof r.kind === "string" ? r.kind : typeof r.type === "string" ? r.type : null;
    if (!kind) continue;
    registered.add(kind);
    const base = byKind.get(kind);
    const schema = r.schema && typeof r.schema === "object" ? (r.schema as StepSchema) : (base?.schema ?? s({}));
    byKind.set(kind, {
      kind,
      label: typeof r.label === "string" ? r.label : (base?.label ?? titleOf(kind)),
      description: typeof r.description === "string" ? r.description : (base?.description ?? ""),
      category: base?.category ?? categoryOf(kind),
      impact: r.impact === "high" ? "high" : r.impact === "low" ? "low" : (base?.impact ?? "low"),
      schema,
      registered: true,
    });
  }
  return [...byKind.values()].map((k) => ({ ...k, registered: registered.has(k.kind) }));
}

export function stepKind(catalog: StepKind[], kind: string): StepKind {
  return catalog.find((k) => k.kind === kind) ?? { kind, label: titleOf(kind), description: "", category: categoryOf(kind), impact: "low", schema: s({}), registered: false };
}

/** Default config from a schema's `default`s. */
export function defaultConfig(schema: StepSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, p] of Object.entries(schema.properties ?? {})) if (p.default !== undefined) out[k] = p.default;
  return out;
}

/** Required fields missing from a step's config. */
export function missingRequired(schema: StepSchema, config: Record<string, unknown>): string[] {
  return (schema.required ?? []).filter((k) => {
    const v = config[k];
    return v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
  });
}
