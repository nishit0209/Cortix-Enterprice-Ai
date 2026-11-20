/**
 * Pure mappers from raw `apps/ml` payloads to the normalized shapes in `./types`.
 *
 * They accept the variants the backend may legitimately send (documented in the frontend W4 handoff), drop
 * anything non-finite, and never synthesize values. Unit-tested in `mappers.test.ts`.
 */

import type {
  ColumnReason,
  ConfusionMatrix,
  Contribution,
  Curve,
  CurvePoint,
  Eda,
  EdaColumn,
  Experiment,
  FeatureDrift,
  FeatureSpec,
  Importance,
  MetricSet,
  MlModel,
  MlProject,
  ModelVersion,
  Monitoring,
  Prediction,
  PredictResult,
  PsiLevel,
  Recommendation,
  ResidualPoint,
  TaskType,
} from "./types";
import { TASK_TYPES } from "./types";

type Obj = Record<string, unknown>;

export const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string | null => (typeof v === "string" ? v : typeof v === "number" ? String(v) : null);

export function num(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** First defined (non-null/undefined) property among `keys`. */
function pick(o: Obj | null | undefined, ...keys: string[]): unknown {
  if (!o) return undefined;
  for (const k of keys) if (o[k] !== undefined && o[k] !== null) return o[k];
  return undefined;
}

/** `{items: [...]}` envelope, `{<key>: [...]}` or a bare array. */
export function listOf(v: unknown, ...keys: string[]): unknown[] {
  if (Array.isArray(v)) return v;
  if (isObj(v)) {
    for (const k of ["items", ...keys]) if (Array.isArray(v[k])) return v[k] as unknown[];
  }
  return [];
}

export function asTaskType(v: unknown): TaskType | null {
  const s = typeof v === "string" ? v.toLowerCase() : "";
  if ((TASK_TYPES as string[]).includes(s)) return s as TaskType;
  if (s === "forecasting" || s === "time_series") return "forecast";
  if (s === "clustering" || s === "cluster") return "clustering";
  return null;
}

/* ---------------- metrics ---------------- */

/** Keep only finite numeric entries of a metrics object (`{f1: 0.8, confusion_matrix: [...]}` -> `{f1: 0.8}`). */
export function numericRecord(v: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (!isObj(v)) return out;
  for (const [k, raw] of Object.entries(v)) {
    const n = num(raw);
    if (n !== null && typeof raw !== "boolean") out[k] = n;
  }
  return out;
}

/**
 * Metrics come either flat (`{accuracy: .9, f1: .8}`) or split (`{test: {...}, train: {...}, validation: {...},
 * cv: {mean: {...}, std: {...}}}` / `{cv_mean: {...}, cv_std: {...}}`). Headline metrics are the test split when
 * present, else validation, else CV mean, else the flat numbers.
 */
export function mapMetrics(raw: unknown): MetricSet {
  const m = isObj(raw) ? raw : {};
  const test = numericRecord(m.test);
  const train = numericRecord(m.train);
  const validation = numericRecord(pick(m, "validation", "val", "holdout"));
  const cv = isObj(m.cv) ? m.cv : {};
  const cv_mean = numericRecord(pick(m, "cv_mean") ?? cv.mean);
  const cv_std = numericRecord(pick(m, "cv_std") ?? cv.std);
  const flat = numericRecord(m);
  const primary = Object.keys(test).length
    ? test
    : Object.keys(validation).length
      ? validation
      : Object.keys(cv_mean).length
        ? cv_mean
        : flat;
  return { primary, train, validation, cv_mean, cv_std };
}

/** Metrics where a lower value is better. Everything else is "higher is better". */
const LOWER_IS_BETTER = new Set(["mae", "mse", "rmse", "mape", "smape", "log_loss", "logloss", "davies_bouldin", "brier"]);
export const lowerIsBetter = (metric: string) => LOWER_IS_BETTER.has(metric.toLowerCase());

/** Default primary metric per task (ML_ARCHITECTURE §6). */
export function defaultPrimaryMetric(task: TaskType | null): string {
  switch (task) {
    case "regression":
      return "rmse";
    case "forecast":
      return "mae";
    case "clustering":
      return "silhouette";
    default:
      return "f1_weighted";
  }
}

/** Resolve a metric name to the key actually present (`f1_weighted` falls back to `f1`, `roc_auc` to `auc`). */
export function metricValue(metrics: Record<string, number>, name: string): number | null {
  const aliases: Record<string, string[]> = {
    f1_weighted: ["f1_weighted", "f1", "f1_macro"],
    f1_macro: ["f1_macro", "f1", "f1_weighted"],
    roc_auc: ["roc_auc", "auc", "roc_auc_ovr"],
    pr_auc: ["pr_auc", "average_precision", "ap"],
    r2: ["r2", "r_squared"],
  };
  for (const k of aliases[name] ?? [name]) if (metrics[k] !== undefined) return metrics[k];
  return null;
}

/** Index of the best experiment for `metric` (respecting direction); -1 when none has it. */
export function bestIndex(metricSets: Array<Record<string, number>>, metric: string): number {
  let best = -1;
  let bestVal: number | null = null;
  metricSets.forEach((m, i) => {
    const v = metricValue(m, metric);
    if (v === null) return;
    if (bestVal === null || (lowerIsBetter(metric) ? v < bestVal : v > bestVal)) {
      best = i;
      bestVal = v;
    }
  });
  return best;
}

/** Rows for a grouped metric bar chart: one row per metric, one key per experiment id. */
export function metricBars(
  experiments: Array<{ id: string; metrics: Record<string, number> }>,
  metrics?: string[],
): Array<Record<string, string | number | null>> {
  const names =
    metrics ??
    Array.from(new Set(experiments.flatMap((e) => Object.keys(e.metrics)))).filter((k) =>
      experiments.some((e) => e.metrics[k] !== undefined),
    );
  return names.map((name) => {
    const row: Record<string, string | number | null> = { metric: name };
    for (const e of experiments) row[e.id] = e.metrics[name] ?? null;
    return row;
  });
}

/* ---------------- curves / matrices / residuals ---------------- */

function pointsFromPairs(xs: unknown, ys: unknown): CurvePoint[] {
  const a = arr(xs);
  const b = arr(ys);
  const out: CurvePoint[] = [];
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const x = num(a[i]);
    const y = num(b[i]);
    if (x !== null && y !== null) out.push({ x, y });
  }
  return out;
}

/**
 * ROC (`{fpr, tpr, auc}`) or PR (`{recall, precision, auc|average_precision}`) curve. Also accepts a list of
 * `{fpr,tpr}` / `{recall,precision}` / `{x,y}` / `[x, y]` points. Points are sorted by x. Returns null when
 * fewer than two valid points remain (a curve is never drawn from one point).
 */
export function mapCurve(raw: unknown, kind: "roc" | "pr"): Curve | null {
  if (raw === null || raw === undefined) return null;
  const [xk, yk] = kind === "roc" ? ["fpr", "tpr"] : ["recall", "precision"];
  let points: CurvePoint[] = [];
  let auc: number | null = null;
  if (isObj(raw)) {
    auc = num(pick(raw, "auc", kind === "pr" ? "average_precision" : "roc_auc", "ap"));
    if (Array.isArray(raw[xk])) points = pointsFromPairs(raw[xk], raw[yk]);
    else if (Array.isArray(raw.x)) points = pointsFromPairs(raw.x, raw.y);
    else if (Array.isArray(raw.points)) {
      const c = mapCurve(raw.points, kind);
      return c ? { ...c, auc: auc ?? c.auc } : null;
    }
  } else if (Array.isArray(raw)) {
    for (const p of raw) {
      if (Array.isArray(p)) {
        const x = num(p[0]);
        const y = num(p[1]);
        if (x !== null && y !== null) points.push({ x, y });
      } else if (isObj(p)) {
        const x = num(pick(p, xk, "x"));
        const y = num(pick(p, yk, "y"));
        if (x !== null && y !== null) points.push({ x, y });
      }
    }
  }
  if (points.length < 2) return null;
  points.sort((a, b) => a.x - b.x || a.y - b.y);
  return { points, auc };
}

/** `{labels, matrix}` / `{labels, values}` or a bare square matrix (labels become "0", "1", ...). */
export function mapConfusion(raw: unknown): ConfusionMatrix | null {
  let labels: string[] = [];
  let rows: unknown[] = [];
  if (Array.isArray(raw)) rows = raw;
  else if (isObj(raw)) {
    rows = arr(pick(raw, "matrix", "values", "data"));
    labels = arr(raw.labels).map((l) => String(l));
  }
  const matrix = rows.map((r) => arr(r).map((v) => num(v) ?? 0));
  const n = matrix.length;
  if (n === 0 || matrix.some((r) => r.length !== n)) return null;
  if (labels.length !== n) labels = matrix.map((_, i) => String(i));
  return { labels, matrix };
}

/** Confusion cells for a heatmap, with the row-normalized share (recall per actual class). */
export function confusionCells(cm: ConfusionMatrix): Array<{ actual: string; predicted: string; count: number; share: number }> {
  const out: Array<{ actual: string; predicted: string; count: number; share: number }> = [];
  cm.matrix.forEach((row, i) => {
    const total = row.reduce((a, b) => a + b, 0);
    row.forEach((count, j) => out.push({ actual: cm.labels[i], predicted: cm.labels[j], count, share: total > 0 ? count / total : 0 }));
  });
  return out;
}

/** `[{predicted, residual}]`, `[{y_pred, y_true}]` or `{predicted:[], residual:[]}` / `{y_pred:[], y_true:[]}`. */
export function mapResiduals(raw: unknown, limit = 1000): ResidualPoint[] {
  const out: ResidualPoint[] = [];
  const push = (p: number | null, r: number | null) => {
    if (p !== null && r !== null && out.length < limit) out.push({ predicted: p, residual: r });
  };
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (!isObj(item)) continue;
      const p = num(pick(item, "predicted", "y_pred", "prediction"));
      let r = num(pick(item, "residual"));
      if (r === null) {
        const actual = num(pick(item, "actual", "y_true"));
        r = actual !== null && p !== null ? actual - p : null;
      }
      push(p, r);
    }
  } else if (isObj(raw)) {
    const preds = arr(pick(raw, "predicted", "y_pred"));
    const res = arr(raw.residual ?? raw.residuals);
    const actual = arr(pick(raw, "actual", "y_true"));
    preds.forEach((pv, i) => {
      const p = num(pv);
      const r = res.length ? num(res[i]) : p !== null && num(actual[i]) !== null ? (num(actual[i]) as number) - p : null;
      push(p, r);
    });
  }
  return out;
}

/** `{feature: value}` or `[{feature|name, importance|value|mean_abs_shap}]`, sorted by |value| desc. */
export function mapImportance(raw: unknown, limit = 20): Importance[] {
  let items: Importance[] = [];
  if (Array.isArray(raw)) {
    for (const r of raw) {
      if (Array.isArray(r)) {
        const v = num(r[1]);
        if (typeof r[0] === "string" && v !== null) items.push({ feature: r[0], value: v });
      } else if (isObj(r)) {
        const f = str(pick(r, "feature", "name", "column"));
        const v = num(pick(r, "importance", "mean_abs_shap", "contribution", "value", "shap", "score"));
        if (f && v !== null) items.push({ feature: f, value: v });
      }
    }
  } else if (isObj(raw)) {
    const inner = pick(raw, "features", "values", "items");
    if (inner !== undefined) return mapImportance(inner, limit);
    items = Object.entries(raw)
      .map(([feature, v]) => ({ feature, value: num(v) }))
      .filter((x): x is Importance => x.value !== null);
  }
  return items.sort((a, b) => Math.abs(b.value) - Math.abs(a.value)).slice(0, limit);
}

/* ---------------- entities ---------------- */

function mapReasons(raw: unknown): ColumnReason[] {
  if (isObj(raw)) return Object.entries(raw).map(([column, reason]) => ({ column, reason: String(reason ?? "") }));
  return arr(raw)
    .map((r) => (typeof r === "string" ? { column: r, reason: "" } : isObj(r) ? { column: str(pick(r, "column", "name")) ?? "", reason: str(pick(r, "reason", "detail")) ?? "" } : null))
    .filter((r): r is ColumnReason => !!r && !!r.column);
}

export function mapProject(raw: unknown): MlProject {
  const p = isObj(raw) ? raw : {};
  const best = isObj(p.best_metric) ? p.best_metric : null;
  const dataset = isObj(p.dataset) ? p.dataset : null;
  return {
    id: String(p.id ?? ""),
    name: str(p.name) ?? "Untitled project",
    description: str(p.description) ?? "",
    dataset_id: String(pick(p, "dataset_id") ?? dataset?.id ?? ""),
    dataset_name: str(pick(p, "dataset_name") ?? dataset?.name) ?? null,
    target_column: str(pick(p, "target_column", "target")),
    task_type: asTaskType(pick(p, "task_type", "objective")) ?? "classification",
    time_column: str(p.time_column),
    features: arr(pick(p, "features", "feature_columns")).map((f) => (isObj(f) ? str(f.name) : str(f))).filter((f): f is string => !!f),
    excluded_by_policy: mapReasons(p.excluded_by_policy),
    leakage_dropped: mapReasons(pick(p, "leakage_dropped", "dropped_leakage", "leakage")),
    primary_metric: str(p.primary_metric),
    experiment_count: num(p.experiment_count),
    model_count: num(p.model_count),
    best_metric: best && str(best.name) && num(best.value) !== null ? { name: str(best.name)!, value: num(best.value)! } : null,
    created_at: str(p.created_at),
    updated_at: str(p.updated_at),
  };
}

function kindOf(dtype: string, raw: Obj): EdaColumn["kind"] {
  const k = str(raw.kind)?.toLowerCase();
  if (k === "numeric" || k === "categorical" || k === "datetime" || k === "boolean" || k === "text") return k;
  const d = dtype.toLowerCase();
  if (/int|float|double|decimal|numeric|number/.test(d)) return "numeric";
  if (/bool/.test(d)) return "boolean";
  if (/date|time/.test(d)) return "datetime";
  if (/str|utf8|cat|object|text|varchar/.test(d)) return "categorical";
  return "other";
}

function mapTopValues(raw: unknown): Array<{ value: string; count: number }> {
  if (isObj(raw)) return Object.entries(raw).map(([value, c]) => ({ value, count: num(c) ?? 0 }));
  return arr(raw)
    .map((t) => (isObj(t) ? { value: String(t.value ?? t.label ?? ""), count: num(pick(t, "count", "n")) ?? 0 } : null))
    .filter((t): t is { value: string; count: number } => !!t);
}

function mapEdaColumn(raw: unknown): EdaColumn | null {
  if (!isObj(raw)) return null;
  const name = str(pick(raw, "name", "column"));
  if (!name) return null;
  const dtype = str(pick(raw, "dtype", "data_type", "type")) ?? "";
  let missingPct = num(pick(raw, "missing_pct", "null_pct", "missing_ratio"));
  if (missingPct !== null && missingPct <= 1 && raw.missing_ratio !== undefined) missingPct *= 100;
  return {
    name,
    dtype,
    kind: kindOf(dtype, raw),
    missing: num(pick(raw, "missing", "null_count", "missing_count")),
    missing_pct: missingPct,
    distinct: num(pick(raw, "distinct", "unique", "n_unique")),
    mean: num(raw.mean),
    std: num(pick(raw, "std", "stddev")),
    min: raw.min ?? null,
    max: raw.max ?? null,
    median: num(pick(raw, "median", "p50")),
    outliers: num(pick(raw, "outliers", "outlier_count")),
    top_values: mapTopValues(pick(raw, "top_values", "top")),
  };
}

function mapDistribution(raw: unknown): Array<{ label: string; count: number }> {
  if (isObj(raw) && Array.isArray(raw.bins) && Array.isArray(raw.counts)) {
    const bins = raw.bins as unknown[];
    const counts = raw.counts as unknown[];
    return counts.map((c, i) => ({ label: String(bins[i] ?? i), count: num(c) ?? 0 }));
  }
  if (isObj(raw)) return Object.entries(raw).map(([label, c]) => ({ label, count: num(c) ?? 0 }));
  return arr(raw)
    .map((d) => (isObj(d) ? { label: String(pick(d, "label", "value", "class", "bin") ?? ""), count: num(pick(d, "count", "n")) ?? 0 } : null))
    .filter((d): d is { label: string; count: number } => !!d);
}

/** Correlation as `{columns, matrix}` or as pairs `[{a, b, value}]` (folded into a symmetric matrix). */
export function mapCorrelation(raw: unknown): Eda["correlation"] {
  if (!raw) return null;
  const o = isObj(raw) ? raw : { pairs: raw };
  const method = str(o.method) ?? "pearson";
  const pearson = isObj(o.pearson) ? o.pearson : null;
  if (pearson) return mapCorrelation({ method: "pearson", ...pearson });
  if (Array.isArray(o.columns) && Array.isArray(o.matrix)) {
    const columns = (o.columns as unknown[]).map(String);
    const matrix = (o.matrix as unknown[]).map((r) => arr(r).map((v) => num(v)));
    if (matrix.length !== columns.length || matrix.some((r) => r.length !== columns.length)) return null;
    return { method, columns, matrix };
  }
  const pairs = arr(o.pairs);
  if (!pairs.length) return null;
  const cols: string[] = [];
  const idx = (c: string) => {
    let i = cols.indexOf(c);
    if (i < 0) i = cols.push(c) - 1;
    return i;
  };
  const vals: Array<[number, number, number]> = [];
  for (const p of pairs) {
    if (!isObj(p)) continue;
    const a = str(pick(p, "a", "x", "col_a")),
      b = str(pick(p, "b", "y", "col_b")),
      v = num(pick(p, "value", "r", "corr"));
    if (a && b && v !== null) vals.push([idx(a), idx(b), v]);
  }
  const matrix: Array<Array<number | null>> = cols.map((_, i) => cols.map((__, j) => (i === j ? 1 : null)));
  for (const [i, j, v] of vals) {
    matrix[i][j] = v;
    matrix[j][i] = v;
  }
  return cols.length ? { method, columns: cols, matrix } : null;
}

export function mapEda(raw: unknown): Eda | null {
  if (!isObj(raw)) return null;
  const e = isObj(raw.eda) ? raw.eda : raw;
  const columns = listOf(pick(e, "columns", "column_stats"), "columns").map(mapEdaColumn).filter((c): c is EdaColumn => !!c);
  const t = isObj(e.target) ? e.target : null;
  const target = t
    ? {
        name: str(pick(t, "name", "column")) ?? "",
        kind: (str(t.kind) === "numeric" || num(t.mean) !== null ? "numeric" : "categorical") as "numeric" | "categorical",
        distribution: mapDistribution(pick(t, "distribution", "class_balance", "histogram", "counts")),
      }
    : null;
  const tc = isObj(e.time_coverage) ? e.time_coverage : null;
  const narr = pick(e, "narration", "summary", "explanation");
  return {
    row_count: num(pick(e, "row_count", "rows", "n_rows")),
    column_count: num(pick(e, "column_count", "n_columns")) ?? (columns.length || null),
    duplicate_rows: num(pick(e, "duplicate_rows", "duplicates")),
    columns,
    target,
    correlation: mapCorrelation(pick(e, "correlation", "correlations")),
    time_coverage: tc
      ? {
          column: str(tc.column) ?? "",
          start: str(pick(tc, "start", "min")),
          end: str(pick(tc, "end", "max")),
          series: arr(pick(tc, "series", "counts", "periods"))
            .map((p) => (isObj(p) ? { period: String(pick(p, "period", "date", "bucket") ?? ""), count: num(pick(p, "count", "n")) ?? 0 } : null))
            .filter((p): p is { period: string; count: number } => !!p && !!p.period),
        }
      : null,
    narration:
      typeof narr === "string" && narr.trim()
        ? { text: narr, source: str(e.narration_source) ?? "local" }
        : isObj(narr) && str(narr.text)
          ? { text: str(narr.text)!, source: str(narr.source) ?? "local" }
          : null,
    warnings: arr(e.warnings).map((w) => (isObj(w) ? str(pick(w, "message", "detail")) : str(w))).filter((w): w is string => !!w),
    computed_at: str(pick(e, "computed_at", "created_at")),
  };
}

export function mapRecommendations(raw: unknown): Recommendation[] {
  return listOf(raw, "recommendations", "candidates")
    .map((r, i): Recommendation | null => {
      if (!isObj(r)) return null;
      const algorithm = str(pick(r, "algorithm", "key", "model"));
      if (!algorithm) return null;
      const reasons = pick(r, "reasons", "reason");
      return {
        algorithm,
        label: str(pick(r, "label", "name", "display_name")) ?? algorithm,
        rank: num(r.rank) ?? i + 1,
        score: num(r.score),
        reasons: (Array.isArray(reasons) ? reasons : reasons ? [reasons] : []).map(String).filter(Boolean),
        warnings: arr(r.warnings).map(String),
        params: isObj(pick(r, "params", "default_params", "hyperparameters")) ? (pick(r, "params", "default_params", "hyperparameters") as Obj) : {},
      };
    })
    .filter((r): r is Recommendation => !!r)
    .sort((a, b) => a.rank - b.rank);
}

export function mapExperiment(raw: unknown): Experiment {
  const e = isObj(raw) ? raw : {};
  const m = isObj(e.metrics) ? e.metrics : {};
  const curves = isObj(e.curves) ? e.curves : {};
  const err = e.error;
  const sample = isObj(pick(e, "sampled", "sample")) ? (pick(e, "sampled", "sample") as Obj) : null;
  return {
    id: String(e.id ?? ""),
    project_id: str(e.project_id),
    algorithm: str(e.algorithm) ?? "unknown",
    status: str(e.status) ?? "queued",
    params: (isObj(pick(e, "hyperparameters", "params")) ? pick(e, "hyperparameters", "params") : {}) as Obj,
    random_state: num(pick(e, "random_state", "random_seed", "seed")),
    metrics: mapMetrics(m),
    confusion: mapConfusion(pick(e, "confusion_matrix") ?? m.confusion_matrix),
    roc: mapCurve(pick(e, "roc_curve") ?? curves.roc ?? m.roc_curve, "roc"),
    pr: mapCurve(pick(e, "pr_curve") ?? curves.pr ?? m.pr_curve, "pr"),
    residuals: mapResiduals(pick(e, "residuals") ?? m.residuals),
    importance: mapImportance(pick(e, "feature_importance", "feature_importances", "importances")),
    shap: mapImportance(pick(e, "shap_summary", "shap")),
    duration_s: num(pick(e, "duration_s", "duration_seconds", "duration")),
    job_id: str(e.job_id),
    error: isObj(err) ? (str(pick(err, "message", "detail", "code")) ?? "Training failed") : str(err),
    sampled: sample && num(pick(sample, "rows", "n")) !== null ? { rows: num(pick(sample, "rows", "n"))!, of: num(pick(sample, "of", "total")) ?? 0 } : null,
    created_at: str(e.created_at),
    finished_at: str(e.finished_at),
  };
}

/* ---------------- registry ---------------- */

function normDtype(v: string | null, categories: string[]): FeatureSpec["dtype"] {
  const d = (v ?? "").toLowerCase();
  if (categories.length || /cat|enum|str|object|utf8/.test(d)) return "category";
  if (/bool/.test(d)) return "boolean";
  if (/^int|integer|int\d+/.test(d)) return "integer";
  if (/float|double|decimal|num|number/.test(d)) return "number";
  if (/date|time/.test(d)) return "datetime";
  return "text";
}

/** Feature schema: `[{name, dtype, categories}]`, `{features|columns: [...]}` or `{name: dtype}`. */
export function mapFeatureSchema(raw: unknown): FeatureSpec[] {
  let items: unknown[] = [];
  if (Array.isArray(raw)) items = raw;
  else if (isObj(raw)) {
    const inner = pick(raw, "features", "columns", "fields");
    if (Array.isArray(inner)) items = inner;
    else items = Object.entries(raw).map(([name, dtype]) => (isObj(dtype) ? { name, ...dtype } : { name, dtype }));
  }
  return items
    .map((f): FeatureSpec | null => {
      if (!isObj(f)) return null;
      const name = str(pick(f, "name", "column"));
      if (!name) return null;
      const categories = arr(pick(f, "categories", "allowed", "values")).map(String);
      return {
        name,
        dtype: normDtype(str(pick(f, "dtype", "type", "kind")), categories),
        categories,
        required: f.required === true,
        min: num(f.min),
        max: num(f.max),
      };
    })
    .filter((f): f is FeatureSpec => !!f);
}

export function mapVersion(raw: unknown): ModelVersion | null {
  if (!isObj(raw)) return null;
  const version = num(pick(raw, "version", "number"));
  if (version === null) return null;
  const promoted = pick(raw, "promoted_by", "approved_by");
  return {
    version,
    stage: str(raw.stage) ?? "none",
    metrics: mapMetrics(raw.metrics).primary,
    algorithm: str(raw.algorithm),
    experiment_id: str(raw.experiment_id),
    feature_schema: mapFeatureSchema(raw.feature_schema),
    artifact_sha256: str(raw.artifact_sha256),
    promoted_by: isObj(promoted) ? str(pick(promoted, "email", "full_name", "id")) : str(promoted),
    promoted_at: str(pick(raw, "promoted_at", "approved_at")),
    created_at: str(raw.created_at),
  };
}

export function mapModel(raw: unknown): MlModel {
  const m = isObj(raw) ? raw : {};
  const versions = listOf(m.versions, "versions").map(mapVersion).filter((v): v is ModelVersion => !!v).sort((a, b) => b.version - a.version);
  const prodRaw = pick(m, "production_version");
  const prod = isObj(prodRaw) ? num(prodRaw.version) : num(prodRaw);
  const latestRaw = pick(m, "latest_version");
  const latest = isObj(latestRaw) ? num(latestRaw.version) : num(latestRaw);
  const project = isObj(m.project) ? m.project : null;
  return {
    id: String(m.id ?? ""),
    name: str(m.name) ?? "Untitled model",
    project_id: str(m.project_id) ?? (project ? str(project.id) : null),
    project_name: str(m.project_name) ?? (project ? str(project.name) : null),
    task_type: asTaskType(m.task_type),
    target_column: str(m.target_column),
    versions,
    production_version: prod ?? versions.find((v) => v.stage === "production")?.version ?? null,
    latest_version: latest ?? versions[0]?.version ?? null,
    created_at: str(m.created_at),
  };
}

/** The version a playground should use: production, else the newest staging, else the newest. */
export function servingVersion(model: MlModel): ModelVersion | null {
  return (
    model.versions.find((v) => v.stage === "production") ??
    model.versions.find((v) => v.stage === "staging") ??
    model.versions[0] ??
    null
  );
}

/* ---------------- prediction ---------------- */

function mapContributions(raw: unknown): Contribution[] {
  return mapImportance(isObj(raw) && (raw.contributions || raw.top) ? (raw.contributions ?? raw.top) : raw, 10).map((i) => ({
    feature: i.feature,
    value: i.value,
  }));
}

export function mapPrediction(raw: unknown): Prediction {
  const p = isObj(raw) ? raw : { prediction: raw };
  const probsRaw = pick(p, "probabilities", "proba");
  const probabilities = isObj(probsRaw)
    ? Object.entries(probsRaw)
        .map(([label, v]) => ({ label, p: num(v) }))
        .filter((x): x is { label: string; p: number } => x.p !== null)
        .sort((a, b) => b.p - a.p)
    : [];
  return {
    value: pick(p, "prediction", "value", "label") ?? null,
    probability: num(pick(p, "probability", "confidence", "score")) ?? probabilities[0]?.p ?? null,
    probabilities,
    contributions: mapContributions(pick(p, "explanation", "contributions", "shap")),
  };
}

export function mapPredictResult(raw: unknown): PredictResult {
  const r = isObj(raw) ? raw : {};
  const preds = listOf(pick(r, "predictions", "results"), "predictions");
  const mv = pick(r, "model_version", "version");
  return {
    predictions: preds.map(mapPrediction),
    model_version: isObj(mv) ? num(mv.version) : num(mv),
    request_id: str(r.request_id),
  };
}

/* ---------------- monitoring ---------------- */

/** PSI thresholds (ML_ARCHITECTURE §9; mission): green < 0.1, amber < 0.25, red otherwise. */
export const PSI_STABLE = 0.1;
export const PSI_MODERATE = 0.25;

export function psiLevel(psi: number): PsiLevel {
  if (psi < PSI_STABLE) return "stable";
  if (psi < PSI_MODERATE) return "moderate";
  return "significant";
}

export const PSI_TONE: Record<PsiLevel, { tone: "success" | "warning" | "danger"; color: string; label: string }> = {
  stable: { tone: "success", color: "var(--success)", label: "Stable" },
  moderate: { tone: "warning", color: "var(--warning)", label: "Moderate shift" },
  significant: { tone: "danger", color: "var(--danger)", label: "Significant drift" },
};

export function mapMonitoring(raw: unknown): Monitoring {
  const m = isObj(raw) ? raw : {};
  const report = isObj(m.latest) ? m.latest : isObj(m.report) ? m.report : m;
  const psiRaw = pick(report, "feature_psi", "psi", "features");
  let features: FeatureDrift[] = [];
  if (isObj(psiRaw)) {
    features = Object.entries(psiRaw)
      .map(([feature, v]) => ({ feature, psi: isObj(v) ? num(v.psi) : num(v) }))
      .filter((x): x is { feature: string; psi: number } => x.psi !== null)
      .map((x) => ({ ...x, level: psiLevel(x.psi) }));
  } else {
    features = arr(psiRaw)
      .map((f) => (isObj(f) ? { feature: str(pick(f, "feature", "name")) ?? "", psi: num(f.psi) } : null))
      .filter((x): x is { feature: string; psi: number } => !!x && !!x.feature && x.psi !== null)
      .map((x) => ({ ...x, level: psiLevel(x.psi) }));
  }
  features.sort((a, b) => b.psi - a.psi);

  const pd = pick(report, "prediction_distribution", "prediction_drift");
  let prediction_distribution: Monitoring["prediction_distribution"] = [];
  let prediction_psi: number | null = null;
  if (isObj(pd)) {
    prediction_psi = num(pd.psi);
    const bins = arr(pick(pd, "bins", "labels"));
    const ref = arr(pick(pd, "reference", "expected", "train"));
    const cur = arr(pick(pd, "current", "actual", "recent"));
    if (bins.length) prediction_distribution = bins.map((b, i) => ({ bin: String(b), reference: num(ref[i]), current: num(cur[i]) }));
    else if (Array.isArray(pd.items)) prediction_distribution = mapBins(pd.items);
  } else if (Array.isArray(pd)) prediction_distribution = mapBins(pd);

  const win = isObj(m.window) ? m.window : report;
  return {
    window: { start: str(pick(win, "start", "window_start")), end: str(pick(win, "end", "window_end")) },
    features,
    prediction_distribution,
    prediction_psi,
    performance: numericRecord(pick(report, "performance")),
    alert: report.alert === true || features.some((f) => f.level === "significant"),
    predictions_count: num(pick(m, "predictions_count", "n_predictions") ?? report.predictions_count),
  };
}

function mapBins(items: unknown[]): Monitoring["prediction_distribution"] {
  return items
    .map((b) => (isObj(b) ? { bin: String(pick(b, "bin", "label") ?? ""), reference: num(pick(b, "reference", "expected")), current: num(pick(b, "current", "actual")) } : null))
    .filter((b): b is Monitoring["prediction_distribution"][number] => !!b);
}
