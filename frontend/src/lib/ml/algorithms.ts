/**
 * Training form configuration: the algorithms `apps/ml` trains per task (W4 AI/ML mission B.5) and their
 * hyperparameters with sane defaults. This is UI configuration (labels, bounds, defaults), not data: what
 * actually trains, and every metric shown afterwards, comes from the API.
 */

import type { TaskType } from "./types";

export type ParamKind = "int" | "float" | "select" | "bool";

export interface ParamSpec {
  key: string;
  label: string;
  kind: ParamKind;
  default: number | string | boolean | null;
  min?: number;
  max?: number;
  step?: number;
  /** `null` allowed ("no limit"), shown as an empty input. */
  nullable?: boolean;
  options?: Array<{ value: string; label: string }>;
  advanced?: boolean;
  help?: string;
}

export interface AlgorithmSpec {
  key: string;
  label: string;
  family: "linear" | "tree" | "boosting" | "time_series" | "clustering";
  tasks: TaskType[];
  description: string;
  params: ParamSpec[];
}

const CLASS_WEIGHT: ParamSpec = {
  key: "class_weight",
  label: "Class weight",
  kind: "select",
  default: "none",
  options: [
    { value: "none", label: "None" },
    { value: "balanced", label: "Balanced" },
  ],
  advanced: true,
  help: "Balanced weights help when the minority class is rare.",
};

const N_ESTIMATORS = (d: number): ParamSpec => ({ key: "n_estimators", label: "Trees", kind: "int", default: d, min: 10, max: 2000, step: 10 });
const MAX_DEPTH = (d: number | null): ParamSpec => ({
  key: "max_depth",
  label: "Max depth",
  kind: "int",
  default: d,
  min: 1,
  max: 64,
  nullable: d === null,
  advanced: true,
  help: d === null ? "Empty = grow until leaves are pure." : undefined,
});
const LEARNING_RATE: ParamSpec = { key: "learning_rate", label: "Learning rate", kind: "float", default: 0.1, min: 0.001, max: 1, step: 0.01 };

export const ALGORITHMS: AlgorithmSpec[] = [
  {
    key: "logistic_regression",
    label: "Logistic regression",
    family: "linear",
    tasks: ["classification"],
    description: "Fast, interpretable baseline with coefficients.",
    params: [
      { key: "C", label: "Inverse regularization (C)", kind: "float", default: 1, min: 0.0001, max: 1000, step: 0.1 },
      { key: "max_iter", label: "Max iterations", kind: "int", default: 1000, min: 100, max: 10000, step: 100, advanced: true },
      CLASS_WEIGHT,
    ],
  },
  {
    key: "ridge",
    label: "Ridge regression",
    family: "linear",
    tasks: ["regression"],
    description: "Regularized linear model; robust to correlated features.",
    params: [{ key: "alpha", label: "Regularization (alpha)", kind: "float", default: 1, min: 0, max: 1000, step: 0.1 }],
  },
  {
    key: "linear_regression",
    label: "Linear regression",
    family: "linear",
    tasks: ["regression"],
    description: "Ordinary least squares baseline.",
    params: [],
  },
  {
    key: "random_forest",
    label: "Random forest",
    family: "tree",
    tasks: ["classification", "regression"],
    description: "Robust ensemble of decision trees; handles nonlinear effects.",
    params: [
      N_ESTIMATORS(200),
      MAX_DEPTH(null),
      { key: "min_samples_leaf", label: "Min samples per leaf", kind: "int", default: 1, min: 1, max: 1000, advanced: true },
      { ...CLASS_WEIGHT, help: "Classification only." },
    ],
  },
  {
    key: "xgboost",
    label: "XGBoost",
    family: "boosting",
    tasks: ["classification", "regression"],
    description: "Gradient-boosted trees; usually the strongest on tabular data.",
    params: [
      N_ESTIMATORS(300),
      LEARNING_RATE,
      MAX_DEPTH(6),
      { key: "subsample", label: "Row subsample", kind: "float", default: 1, min: 0.1, max: 1, step: 0.05, advanced: true },
      { key: "colsample_bytree", label: "Column subsample", kind: "float", default: 1, min: 0.1, max: 1, step: 0.05, advanced: true },
    ],
  },
  {
    key: "hist_gradient_boosting",
    label: "Histogram gradient boosting",
    family: "boosting",
    tasks: ["classification", "regression"],
    description: "scikit-learn's fast boosting; handles missing values natively.",
    params: [
      { key: "max_iter", label: "Boosting iterations", kind: "int", default: 200, min: 10, max: 2000, step: 10 },
      LEARNING_RATE,
      MAX_DEPTH(null),
      { key: "l2_regularization", label: "L2 regularization", kind: "float", default: 0, min: 0, max: 100, step: 0.1, advanced: true },
    ],
  },
  {
    key: "seasonal_naive",
    label: "Seasonal naive (baseline)",
    family: "time_series",
    tasks: ["forecast"],
    description: "Repeats the last season; the baseline every forecast must beat.",
    params: [
      { key: "season_length", label: "Season length", kind: "int", default: 7, min: 1, max: 366 },
      { key: "horizon", label: "Horizon (periods)", kind: "int", default: 14, min: 1, max: 365 },
    ],
  },
  {
    key: "exponential_smoothing",
    label: "Exponential smoothing (Holt-Winters)",
    family: "time_series",
    tasks: ["forecast"],
    description: "Trend and seasonality with prediction intervals.",
    params: [
      { key: "horizon", label: "Horizon (periods)", kind: "int", default: 14, min: 1, max: 365 },
      { key: "seasonal_periods", label: "Seasonal periods", kind: "int", default: 7, min: 2, max: 366 },
      {
        key: "trend",
        label: "Trend",
        kind: "select",
        default: "add",
        options: [
          { value: "none", label: "None" },
          { value: "add", label: "Additive" },
          { value: "mul", label: "Multiplicative" },
        ],
        advanced: true,
      },
      {
        key: "seasonal",
        label: "Seasonality",
        kind: "select",
        default: "add",
        options: [
          { value: "none", label: "None" },
          { value: "add", label: "Additive" },
          { value: "mul", label: "Multiplicative" },
        ],
        advanced: true,
      },
    ],
  },
  {
    key: "sarimax",
    label: "SARIMAX",
    family: "time_series",
    tasks: ["forecast"],
    description: "Seasonal ARIMA with exogenous regressors.",
    params: [
      { key: "horizon", label: "Horizon (periods)", kind: "int", default: 14, min: 1, max: 365 },
      { key: "p", label: "AR order (p)", kind: "int", default: 1, min: 0, max: 5, advanced: true },
      { key: "d", label: "Differencing (d)", kind: "int", default: 1, min: 0, max: 2, advanced: true },
      { key: "q", label: "MA order (q)", kind: "int", default: 1, min: 0, max: 5, advanced: true },
      { key: "seasonal_periods", label: "Seasonal periods", kind: "int", default: 7, min: 0, max: 366, advanced: true },
    ],
  },
  {
    key: "kmeans",
    label: "K-means",
    family: "clustering",
    tasks: ["clustering"],
    description: "Picks k by silhouette over a range.",
    params: [
      { key: "k_min", label: "Min clusters", kind: "int", default: 2, min: 2, max: 20 },
      { key: "k_max", label: "Max clusters", kind: "int", default: 10, min: 2, max: 30 },
    ],
  },
];

const ALIASES: Record<string, string> = {
  logistic: "logistic_regression",
  logreg: "logistic_regression",
  logisticregression: "logistic_regression",
  random_forest_classifier: "random_forest",
  random_forest_regressor: "random_forest",
  randomforest: "random_forest",
  randomforestclassifier: "random_forest",
  randomforestregressor: "random_forest",
  rf: "random_forest",
  xgb: "xgboost",
  xgb_classifier: "xgboost",
  xgb_regressor: "xgboost",
  xgbclassifier: "xgboost",
  xgbregressor: "xgboost",
  hist_gradient_boosting_classifier: "hist_gradient_boosting",
  hist_gradient_boosting_regressor: "hist_gradient_boosting",
  histgradientboosting: "hist_gradient_boosting",
  histgradientboostingclassifier: "hist_gradient_boosting",
  histgradientboostingregressor: "hist_gradient_boosting",
  hgb: "hist_gradient_boosting",
  ridge_regression: "ridge",
  linear: "linear_regression",
  ols: "linear_regression",
  holt_winters: "exponential_smoothing",
  ets: "exponential_smoothing",
  arima: "sarimax",
  sarima: "sarimax",
  naive: "seasonal_naive",
  k_means: "kmeans",
};

/** Canonical catalog key for an algorithm name sent by the API (case and separators ignored). */
export function canonicalAlgorithm(key: string): string {
  const k = key.trim().toLowerCase().replace(/[\s-]+/g, "_");
  if (ALGORITHMS.some((a) => a.key === k)) return k;
  return ALIASES[k] ?? ALIASES[k.replace(/_/g, "")] ?? k;
}

export function algorithmSpec(key: string): AlgorithmSpec | undefined {
  const k = canonicalAlgorithm(key);
  return ALGORITHMS.find((a) => a.key === k);
}

export function algorithmLabel(key: string): string {
  return algorithmSpec(key)?.label ?? key.replace(/_/g, " ");
}

export function algorithmsFor(task: TaskType): AlgorithmSpec[] {
  return ALGORITHMS.filter((a) => a.tasks.includes(task));
}

/** Default hyperparameters of an algorithm, overlaid with recommended values that the spec knows. */
export function defaultParams(spec: AlgorithmSpec, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of spec.params) {
    const o = overrides[p.key];
    out[p.key] = o !== undefined && coerceParam(p, o).ok ? coerceParam(p, o).value : p.default;
  }
  return out;
}

/** Validate/coerce one hyperparameter value against its spec. */
export function coerceParam(p: ParamSpec, raw: unknown): { ok: true; value: unknown } | { ok: false; error: string } {
  if (raw === "" || raw === null || raw === undefined) {
    return p.nullable ? { ok: true, value: null } : { ok: false, error: `${p.label} is required.` };
  }
  if (p.kind === "bool") return { ok: true, value: raw === true || raw === "true" };
  if (p.kind === "select") {
    const v = String(raw);
    return p.options?.some((o) => o.value === v) ? { ok: true, value: v } : { ok: false, error: `${p.label}: choose an option.` };
  }
  const n = typeof raw === "number" ? raw : Number(String(raw).trim());
  if (!Number.isFinite(n)) return { ok: false, error: `${p.label} must be a number.` };
  if (p.kind === "int" && !Number.isInteger(n)) return { ok: false, error: `${p.label} must be a whole number.` };
  if (p.min !== undefined && n < p.min) return { ok: false, error: `${p.label} must be at least ${p.min}.` };
  if (p.max !== undefined && n > p.max) return { ok: false, error: `${p.label} must be at most ${p.max}.` };
  return { ok: true, value: n };
}

/** Validate every parameter; returns the request body values or the per-key errors. */
export function validateParams(spec: AlgorithmSpec, values: Record<string, unknown>): { values: Record<string, unknown>; errors: Record<string, string> } {
  const out: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const p of spec.params) {
    const r = coerceParam(p, values[p.key]);
    if (r.ok) {
      // "none" selects mean "unset" for the backend (e.g. class_weight=None).
      out[p.key] = p.kind === "select" && r.value === "none" ? null : r.value;
    } else errors[p.key] = r.error;
  }
  if (spec.key === "kmeans" && !errors.k_min && !errors.k_max && Number(out.k_min) > Number(out.k_max)) {
    errors.k_max = "Max clusters must be at least min clusters.";
  }
  return { values: out, errors };
}
