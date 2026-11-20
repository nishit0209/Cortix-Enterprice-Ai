import { describe, expect, it } from "vitest";
import {
  bestIndex,
  confusionCells,
  mapConfusion,
  mapCorrelation,
  mapCurve,
  mapEda,
  mapExperiment,
  mapFeatureSchema,
  mapImportance,
  mapMetrics,
  mapModel,
  mapMonitoring,
  mapPredictResult,
  mapProject,
  mapRecommendations,
  mapResiduals,
  metricBars,
  metricValue,
  psiLevel,
  PSI_TONE,
  servingVersion,
} from "./mappers";
import { algorithmLabel, algorithmsFor, canonicalAlgorithm, defaultParams, validateParams, algorithmSpec } from "./algorithms";

describe("metrics mappers", () => {
  it("prefers the test split, then validation, then CV mean, then flat numbers", () => {
    const split = mapMetrics({ test: { f1: 0.81, accuracy: 0.9 }, train: { f1: 0.95 }, cv: { mean: { f1: 0.8 }, std: { f1: 0.02 } } });
    expect(split.primary).toEqual({ f1: 0.81, accuracy: 0.9 });
    expect(split.train).toEqual({ f1: 0.95 });
    expect(split.cv_mean).toEqual({ f1: 0.8 });
    expect(split.cv_std).toEqual({ f1: 0.02 });
    expect(mapMetrics({ validation: { rmse: 3 }, cv_mean: { rmse: 4 } }).primary).toEqual({ rmse: 3 });
    expect(mapMetrics({ cv_mean: { rmse: 4 } }).primary).toEqual({ rmse: 4 });
    expect(mapMetrics({ rmse: 2.5, r2: "0.7", confusion_matrix: [[1]], ok: true, bad: Infinity }).primary).toEqual({ rmse: 2.5, r2: 0.7 });
    expect(mapMetrics(null).primary).toEqual({});
  });

  it("resolves metric aliases and picks the best experiment respecting direction", () => {
    expect(metricValue({ f1: 0.7 }, "f1_weighted")).toBe(0.7);
    expect(metricValue({ auc: 0.9 }, "roc_auc")).toBe(0.9);
    expect(metricValue({}, "rmse")).toBeNull();
    expect(bestIndex([{ rmse: 3 }, { rmse: 2 }, {}], "rmse")).toBe(1);
    expect(bestIndex([{ f1: 0.6 }, { f1: 0.8 }], "f1_weighted")).toBe(1);
    expect(bestIndex([{}, {}], "f1")).toBe(-1);
  });

  it("builds grouped bar rows per metric with nulls where an experiment lacks the metric", () => {
    const rows = metricBars([
      { id: "a", metrics: { f1: 0.7, accuracy: 0.8 } },
      { id: "b", metrics: { f1: 0.9 } },
    ]);
    expect(rows).toEqual([
      { metric: "f1", a: 0.7, b: 0.9 },
      { metric: "accuracy", a: 0.8, b: null },
    ]);
  });
});

describe("curve, matrix and residual mappers", () => {
  it("maps ROC from parallel arrays and PR from point objects, sorted by x", () => {
    const roc = mapCurve({ fpr: [0, 0.5, 0.1, 1], tpr: [0, 0.9, 0.6, 1], auc: 0.88 }, "roc")!;
    expect(roc.auc).toBe(0.88);
    expect(roc.points.map((p) => p.x)).toEqual([0, 0.1, 0.5, 1]);
    const pr = mapCurve([{ recall: 1, precision: 0.5 }, { recall: 0, precision: 1 }, [0.5, 0.8]], "pr")!;
    expect(pr.points).toEqual([{ x: 0, y: 1 }, { x: 0.5, y: 0.8 }, { x: 1, y: 0.5 }]);
    expect(mapCurve({ points: [[0, 0], [1, 1]], auc: 0.5 }, "roc")).toEqual({ points: [{ x: 0, y: 0 }, { x: 1, y: 1 }], auc: 0.5 });
  });

  it("never draws a curve from fewer than two valid points", () => {
    expect(mapCurve({ fpr: [0], tpr: [0] }, "roc")).toBeNull();
    expect(mapCurve({ fpr: [0, "x"], tpr: [0, 1] }, "roc")).toBeNull();
    expect(mapCurve(null, "pr")).toBeNull();
  });

  it("maps confusion matrices and computes row shares", () => {
    const cm = mapConfusion({ labels: ["no", "yes"], matrix: [[80, 20], [10, 90]] })!;
    expect(cm.labels).toEqual(["no", "yes"]);
    const cells = confusionCells(cm);
    expect(cells).toHaveLength(4);
    expect(cells[1]).toEqual({ actual: "no", predicted: "yes", count: 20, share: 0.2 });
    expect(mapConfusion([[1, 2], [3, 4]])!.labels).toEqual(["0", "1"]);
    expect(mapConfusion([[1, 2], [3]])).toBeNull();
    expect(mapConfusion({ labels: ["a"], matrix: [] })).toBeNull();
  });

  it("maps residuals from points, parallel arrays, or actual/predicted pairs", () => {
    expect(mapResiduals([{ predicted: 10, residual: -1 }, { y_pred: 5, y_true: 7 }])).toEqual([
      { predicted: 10, residual: -1 },
      { predicted: 5, residual: 2 },
    ]);
    expect(mapResiduals({ predicted: [1, 2], residual: [0.5, -0.5] })).toEqual([
      { predicted: 1, residual: 0.5 },
      { predicted: 2, residual: -0.5 },
    ]);
    expect(mapResiduals({ y_pred: [1, 2], y_true: [2, 2] })).toEqual([
      { predicted: 1, residual: 1 },
      { predicted: 2, residual: 0 },
    ]);
    expect(mapResiduals(Array.from({ length: 50 }, (_, i) => ({ predicted: i, residual: i })), 10)).toHaveLength(10);
  });

  it("maps importances from objects, lists and pairs, sorted by magnitude", () => {
    expect(mapImportance({ a: 0.1, b: -0.5, c: "x" })).toEqual([{ feature: "b", value: -0.5 }, { feature: "a", value: 0.1 }]);
    expect(mapImportance([{ feature: "x", mean_abs_shap: 0.2 }, { name: "y", importance: 0.4 }, ["z", 0.3]])).toEqual([
      { feature: "y", value: 0.4 },
      { feature: "z", value: 0.3 },
      { feature: "x", value: 0.2 },
    ]);
    expect(mapImportance({ features: [{ feature: "k", value: 1 }] })).toEqual([{ feature: "k", value: 1 }]);
  });
});

describe("entity mappers", () => {
  it("maps a full experiment payload", () => {
    const e = mapExperiment({
      id: "e1",
      algorithm: "xgboost",
      status: "succeeded",
      hyperparameters: { n_estimators: 300 },
      random_state: 42,
      metrics: { test: { roc_auc: 0.86 }, confusion_matrix: { labels: ["0", "1"], matrix: [[5, 1], [2, 7]] } },
      roc_curve: { fpr: [0, 1], tpr: [0, 1] },
      feature_importance: { tenure: 0.4 },
      shap_summary: [{ feature: "tenure", mean_abs_shap: 0.3 }],
      error: null,
      sampled: { rows: 2000000, of: 3500000 },
    });
    expect(e.metrics.primary).toEqual({ roc_auc: 0.86 });
    expect(e.confusion?.matrix).toEqual([[5, 1], [2, 7]]);
    expect(e.roc?.points).toHaveLength(2);
    expect(e.pr).toBeNull();
    expect(e.importance).toEqual([{ feature: "tenure", value: 0.4 }]);
    expect(e.shap).toEqual([{ feature: "tenure", value: 0.3 }]);
    expect(e.random_state).toBe(42);
    expect(e.sampled).toEqual({ rows: 2000000, of: 3500000 });
    expect(mapExperiment({ id: "e2", status: "failed", error: { code: "x", message: "Boom" } }).error).toBe("Boom");
  });

  it("maps projects with policy/leakage reasons in either shape", () => {
    const p = mapProject({
      id: "p1",
      name: "Churn",
      dataset: { id: "d1", name: "Customers" },
      target_column: "churned",
      task_type: "Classification",
      features: [{ name: "tenure" }, "region"],
      excluded_by_policy: { email: "Masked (mask_hash)" },
      leakage_dropped: [{ column: "customer_id", reason: "Unique per row" }, "order_id"],
    });
    expect(p.dataset_id).toBe("d1");
    expect(p.dataset_name).toBe("Customers");
    expect(p.task_type).toBe("classification");
    expect(p.features).toEqual(["tenure", "region"]);
    expect(p.excluded_by_policy).toEqual([{ column: "email", reason: "Masked (mask_hash)" }]);
    expect(p.leakage_dropped.map((r) => r.column)).toEqual(["customer_id", "order_id"]);
    expect(mapProject({ objective: "forecasting" }).task_type).toBe("forecast");
  });

  it("maps EDA: columns, target distribution, correlation pairs and time coverage", () => {
    const eda = mapEda({
      row_count: 100,
      columns: [{ name: "age", dtype: "Int64", missing_pct: 5, mean: 40, top_values: { "30": 4 } }, { column: "region", type: "Utf8" }],
      target: { name: "churned", distribution: { yes: 20, no: 80 } },
      correlations: [{ a: "age", b: "spend", value: 0.4 }],
      time_coverage: { column: "day", start: "2026-01-01", end: "2026-03-01", series: [{ period: "2026-01", count: 10 }] },
      narration: { text: "Balanced enough.", source: "local" },
      warnings: ["High cardinality: city"],
    })!;
    expect(eda.columns.map((c) => [c.name, c.kind])).toEqual([["age", "numeric"], ["region", "categorical"]]);
    expect(eda.columns[0].top_values).toEqual([{ value: "30", count: 4 }]);
    expect(eda.target?.distribution).toEqual([{ label: "yes", count: 20 }, { label: "no", count: 80 }]);
    expect(eda.correlation).toEqual({ method: "pearson", columns: ["age", "spend"], matrix: [[1, 0.4], [0.4, 1]] });
    expect(eda.time_coverage?.series).toEqual([{ period: "2026-01", count: 10 }]);
    expect(eda.narration?.text).toBe("Balanced enough.");
    expect(eda.warnings).toEqual(["High cardinality: city"]);
    expect(mapEda(null)).toBeNull();
  });

  it("rejects non-square correlation matrices", () => {
    expect(mapCorrelation({ columns: ["a", "b"], matrix: [[1, 0.2]] })).toBeNull();
    expect(mapCorrelation({ pearson: { columns: ["a"], matrix: [[1]] } })).toEqual({ method: "pearson", columns: ["a"], matrix: [[1]] });
  });

  it("maps recommendations sorted by rank with reasons", () => {
    const recs = mapRecommendations({
      items: [
        { algorithm: "random_forest", rank: 2, reason: "Nonlinear effects" },
        { algorithm: "xgboost", rank: 1, reasons: ["> 5k rows", "mixed types"], params: { n_estimators: 400 } },
        { label: "missing algorithm" },
      ],
    });
    expect(recs.map((r) => r.algorithm)).toEqual(["xgboost", "random_forest"]);
    expect(recs[0].reasons).toEqual(["> 5k rows", "mixed types"]);
    expect(recs[1].reasons).toEqual(["Nonlinear effects"]);
    expect(recs[0].params).toEqual({ n_estimators: 400 });
  });

  it("maps models, versions and feature schemas; serving version prefers production", () => {
    const m = mapModel({
      id: "m1",
      name: "Churn",
      versions: [
        { version: 1, stage: "archived", metrics: { test: { f1: 0.7 } }, feature_schema: { features: [{ name: "region", dtype: "Utf8", categories: ["N", "S"] }] } },
        { version: 3, stage: "staging", metrics: { f1: 0.8 }, feature_schema: [{ name: "tenure", dtype: "Int64", min: 0 }] },
        { version: 2, stage: "production", metrics: { f1: 0.75 }, feature_schema: { spend: "Float64", vip: "Boolean", seen: { dtype: "Date" } } },
      ],
    });
    expect(m.versions.map((v) => v.version)).toEqual([3, 2, 1]);
    expect(m.production_version).toBe(2);
    expect(m.latest_version).toBe(3);
    expect(servingVersion(m)?.version).toBe(2);
    expect(m.versions[2].metrics).toEqual({ f1: 0.7 });
    expect(m.versions[2].feature_schema[0]).toMatchObject({ name: "region", dtype: "category", categories: ["N", "S"] });
    expect(m.versions[0].feature_schema[0]).toMatchObject({ name: "tenure", dtype: "integer", min: 0 });
    expect(m.versions[1].feature_schema.map((f) => f.dtype)).toEqual(["number", "boolean", "datetime"]);
    expect(servingVersion(mapModel({ versions: [{ version: 1, stage: "none" }, { version: 2, stage: "staging" }] }))?.version).toBe(2);
    expect(mapFeatureSchema(null)).toEqual([]);
  });

  it("maps prediction results with probabilities and SHAP contributions", () => {
    const r = mapPredictResult({
      predictions: [
        { prediction: "yes", probabilities: { no: 0.3, yes: 0.7 }, explanation: [{ feature: "tenure", contribution: -0.2 }, { feature: "spend", value: 0.5 }] },
        { value: 12.5, explanation: { contributions: { a: 0.1 } } },
      ],
      model_version: { version: 4 },
      request_id: "r1",
    });
    expect(r.model_version).toBe(4);
    expect(r.predictions[0].value).toBe("yes");
    expect(r.predictions[0].probability).toBe(0.7);
    expect(r.predictions[0].probabilities[0]).toEqual({ label: "yes", p: 0.7 });
    expect(r.predictions[0].contributions).toEqual([{ feature: "spend", value: 0.5 }, { feature: "tenure", value: -0.2 }]);
    expect(r.predictions[1]).toMatchObject({ value: 12.5, probability: null, contributions: [{ feature: "a", value: 0.1 }] });
  });
});

describe("PSI colour thresholds", () => {
  it("is green below 0.1, amber below 0.25, red from 0.25", () => {
    expect(psiLevel(0)).toBe("stable");
    expect(psiLevel(0.0999)).toBe("stable");
    expect(psiLevel(0.1)).toBe("moderate");
    expect(psiLevel(0.2499)).toBe("moderate");
    expect(psiLevel(0.25)).toBe("significant");
    expect(psiLevel(3)).toBe("significant");
    expect(PSI_TONE.stable.tone).toBe("success");
    expect(PSI_TONE.moderate.tone).toBe("warning");
    expect(PSI_TONE.significant.tone).toBe("danger");
  });

  it("maps monitoring payloads, sorts features by PSI and raises the alert on red drift", () => {
    const m = mapMonitoring({
      window_start: "2026-11-01",
      window_end: "2026-11-08",
      feature_psi: { tenure: 0.05, spend: 0.31, region: { psi: 0.12 } },
      prediction_distribution: { bins: ["0-0.5", "0.5-1"], reference: [0.6, 0.4], current: [0.5, 0.5], psi: 0.04 },
      performance: { f1: 0.7, note: "x" },
    });
    expect(m.features.map((f) => [f.feature, f.level])).toEqual([
      ["spend", "significant"],
      ["region", "moderate"],
      ["tenure", "stable"],
    ]);
    expect(m.alert).toBe(true);
    expect(m.prediction_psi).toBe(0.04);
    expect(m.prediction_distribution).toEqual([
      { bin: "0-0.5", reference: 0.6, current: 0.5 },
      { bin: "0.5-1", reference: 0.4, current: 0.5 },
    ]);
    expect(m.performance).toEqual({ f1: 0.7 });
    expect(m.window).toEqual({ start: "2026-11-01", end: "2026-11-08" });
    const quiet = mapMonitoring({ latest: { feature_psi: [{ feature: "a", psi: 0.01 }], alert: false } });
    expect(quiet.alert).toBe(false);
    expect(quiet.features[0].level).toBe("stable");
  });
});

describe("algorithm catalog", () => {
  it("canonicalizes backend algorithm names", () => {
    expect(canonicalAlgorithm("XGBClassifier")).toBe("xgboost");
    expect(canonicalAlgorithm("random_forest_regressor")).toBe("random_forest");
    expect(canonicalAlgorithm("HistGradientBoostingClassifier")).toBe("hist_gradient_boosting");
    expect(canonicalAlgorithm("Holt-Winters")).toBe("exponential_smoothing");
    expect(canonicalAlgorithm("something_new")).toBe("something_new");
    expect(algorithmLabel("xgb")).toBe("XGBoost");
    expect(algorithmLabel("something_new")).toBe("something new");
  });

  it("offers the right algorithms per task", () => {
    expect(algorithmsFor("classification").map((a) => a.key)).toEqual(["logistic_regression", "random_forest", "xgboost", "hist_gradient_boosting"]);
    expect(algorithmsFor("clustering").map((a) => a.key)).toEqual(["kmeans"]);
    expect(algorithmsFor("forecast")).toHaveLength(3);
  });

  it("validates hyperparameters with defaults, recommended overrides and bounds", () => {
    const xgb = algorithmSpec("xgboost")!;
    expect(defaultParams(xgb, { n_estimators: 400, learning_rate: 99 })).toMatchObject({ n_estimators: 400, learning_rate: 0.1 });
    expect(validateParams(xgb, { ...defaultParams(xgb), n_estimators: "12.5" }).errors.n_estimators).toMatch(/whole number/);
    expect(validateParams(xgb, { ...defaultParams(xgb), learning_rate: "0" }).errors.learning_rate).toMatch(/at least/);
    const rf = algorithmSpec("random_forest")!;
    const ok = validateParams(rf, defaultParams(rf));
    expect(ok.errors).toEqual({});
    expect(ok.values).toEqual({ n_estimators: 200, max_depth: null, min_samples_leaf: 1, class_weight: null });
    const km = algorithmSpec("kmeans")!;
    expect(validateParams(km, { k_min: 6, k_max: 3 }).errors.k_max).toMatch(/at least/);
  });
});
