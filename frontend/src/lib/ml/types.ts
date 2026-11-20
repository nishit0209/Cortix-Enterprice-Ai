/**
 * Normalized ML Workspace shapes (API_SPECIFICATION Phase 9, ML_ARCHITECTURE, CONTRACT "W4 additions").
 *
 * The backend (`apps/ml`, AI/ML agent) is built in parallel with this client. Raw payloads are typed loosely
 * (`Raw*`) and turned into these shapes by `lib/ml/mappers.ts`, which accepts the documented variants
 * (flat or split metrics, curves as point lists or as parallel arrays, ...). Nothing here is invented: every
 * value comes from a response; a missing field stays missing.
 */

import type { ISODate, UUID } from "@/lib/types";

export type TaskType = "classification" | "regression" | "forecast" | "clustering";
export const TASK_TYPES: TaskType[] = ["classification", "regression", "forecast", "clustering"];

export type ExperimentStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type ModelStage = "none" | "staging" | "production" | "archived";

export interface ColumnReason {
  column: string;
  reason: string;
}

export interface MlProject {
  id: UUID;
  name: string;
  description: string;
  dataset_id: UUID;
  dataset_name: string | null;
  target_column: string | null;
  task_type: TaskType;
  time_column: string | null;
  features: string[];
  excluded_by_policy: ColumnReason[];
  leakage_dropped: ColumnReason[];
  primary_metric: string | null;
  experiment_count: number | null;
  model_count: number | null;
  best_metric: { name: string; value: number } | null;
  created_at: ISODate | null;
  updated_at: ISODate | null;
}

/** POST /ml/projects body. */
export interface CreateProjectRequest {
  name: string;
  description?: string;
  dataset_id: UUID;
  target_column: string | null;
  task_type: TaskType;
  time_column?: string | null;
  features: string[];
}

/** Server-side feature preview for the wizard (policy + leakage), see `useProjectPreview`. */
export interface FeaturePreview {
  detected_task_type: TaskType | null;
  candidates: string[];
  excluded_by_policy: ColumnReason[];
  leakage_dropped: ColumnReason[];
  /** true when the preview came from the server; false when computed locally from the profile. */
  server: boolean;
}

/* ---------------- EDA ---------------- */

export interface EdaColumn {
  name: string;
  dtype: string;
  kind: "numeric" | "categorical" | "datetime" | "boolean" | "text" | "other";
  missing: number | null;
  missing_pct: number | null;
  distinct: number | null;
  mean: number | null;
  std: number | null;
  min: unknown;
  max: unknown;
  median: number | null;
  outliers: number | null;
  top_values: Array<{ value: string; count: number }>;
}

export interface Eda {
  row_count: number | null;
  column_count: number | null;
  duplicate_rows: number | null;
  columns: EdaColumn[];
  target: { name: string; kind: "categorical" | "numeric"; distribution: Array<{ label: string; count: number }> } | null;
  correlation: { method: string; columns: string[]; matrix: Array<Array<number | null>> } | null;
  time_coverage: { column: string; start: string | null; end: string | null; series: Array<{ period: string; count: number }> } | null;
  narration: { text: string; source: string } | null;
  warnings: string[];
  computed_at: ISODate | null;
}

/* ---------------- recommendations / experiments ---------------- */

export interface Recommendation {
  algorithm: string;
  label: string;
  rank: number;
  score: number | null;
  reasons: string[];
  warnings: string[];
  params: Record<string, unknown>;
}

export interface CurvePoint {
  x: number;
  y: number;
}

export interface Curve {
  points: CurvePoint[];
  auc: number | null;
}

export interface ConfusionMatrix {
  labels: string[];
  matrix: number[][];
}

export interface ResidualPoint {
  predicted: number;
  residual: number;
}

export interface Importance {
  feature: string;
  value: number;
}

export interface MetricSet {
  /** Headline metrics (test split when available). */
  primary: Record<string, number>;
  train: Record<string, number>;
  validation: Record<string, number>;
  cv_mean: Record<string, number>;
  cv_std: Record<string, number>;
}

export interface Experiment {
  id: UUID;
  project_id: UUID | null;
  algorithm: string;
  status: ExperimentStatus | string;
  params: Record<string, unknown>;
  random_state: number | null;
  metrics: MetricSet;
  confusion: ConfusionMatrix | null;
  roc: Curve | null;
  pr: Curve | null;
  residuals: ResidualPoint[];
  importance: Importance[];
  shap: Importance[];
  duration_s: number | null;
  job_id: UUID | null;
  error: string | null;
  sampled: { rows: number; of: number } | null;
  created_at: ISODate | null;
  finished_at: ISODate | null;
}

/* ---------------- registry ---------------- */

export interface FeatureSpec {
  name: string;
  dtype: "number" | "integer" | "boolean" | "category" | "datetime" | "text";
  categories: string[];
  required: boolean;
  min: number | null;
  max: number | null;
}

export interface ModelVersion {
  version: number;
  stage: ModelStage | string;
  metrics: Record<string, number>;
  algorithm: string | null;
  experiment_id: UUID | null;
  feature_schema: FeatureSpec[];
  artifact_sha256: string | null;
  promoted_by: string | null;
  promoted_at: ISODate | null;
  created_at: ISODate | null;
}

export interface MlModel {
  id: UUID;
  name: string;
  project_id: UUID | null;
  project_name: string | null;
  task_type: TaskType | null;
  target_column: string | null;
  versions: ModelVersion[];
  production_version: number | null;
  latest_version: number | null;
  created_at: ISODate | null;
}

/* ---------------- prediction ---------------- */

export interface Contribution {
  feature: string;
  value: number;
}

export interface Prediction {
  value: unknown;
  probability: number | null;
  probabilities: Array<{ label: string; p: number }>;
  contributions: Contribution[];
}

export interface PredictResult {
  predictions: Prediction[];
  model_version: number | null;
  request_id: string | null;
}

/* ---------------- monitoring ---------------- */

export type PsiLevel = "stable" | "moderate" | "significant";

export interface FeatureDrift {
  feature: string;
  psi: number;
  level: PsiLevel;
}

export interface Monitoring {
  window: { start: ISODate | null; end: ISODate | null };
  features: FeatureDrift[];
  prediction_distribution: Array<{ bin: string; reference: number | null; current: number | null }>;
  prediction_psi: number | null;
  performance: Record<string, number>;
  alert: boolean;
  predictions_count: number | null;
}
