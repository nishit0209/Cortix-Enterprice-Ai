import { describe, expect, it } from "vitest";
import {
  blockedColumns,
  detectTask,
  initialWizard,
  isIdLike,
  localPreview,
  stepError,
  toCreateRequest,
  wizardReducer,
  type WizardAction,
  type WizardColumn,
  type WizardState,
} from "./wizard";

const COLS: WizardColumn[] = [
  { name: "customer_id", dtype: "Utf8", distinct: 1000 },
  { name: "region", dtype: "Utf8", distinct: 4 },
  { name: "tenure_months", dtype: "Int64", distinct: 60 },
  { name: "monthly_spend", dtype: "Float64", distinct: 940 },
  { name: "churned", dtype: "Boolean", distinct: 2 },
  { name: "signup_date", dtype: "Date", distinct: 700 },
  { name: "paid", dtype: "Int64", distinct: 2 },
];

const run = (actions: WizardAction[], from: WizardState = initialWizard) => actions.reduce(wizardReducer, from);

const withDataset = () =>
  run([
    { type: "select_dataset", id: "ds1", name: "Customers", rowCount: 1000 },
    { type: "columns_loaded", datasetId: "ds1", columns: COLS },
  ]);

describe("task detection and leakage heuristics", () => {
  it("detects classification for categorical/boolean/few-level targets and regression for continuous ones", () => {
    expect(detectTask(COLS[4], 1000)).toBe("classification");
    expect(detectTask(COLS[1], 1000)).toBe("classification");
    expect(detectTask(COLS[3], 1000)).toBe("regression");
    expect(detectTask({ name: "rating", dtype: "Int64", distinct: 5 }, 1000)).toBe("classification");
    expect(detectTask({ name: "units", dtype: "Int64", distinct: 30 }, 100000)).toBe("classification");
    expect(detectTask({ name: "units", dtype: "Int64", distinct: 800 }, 1000)).toBe("regression");
    expect(detectTask(undefined, 1000)).toBeNull();
  });

  it("flags id-named and unique-per-row columns but not words ending in 'id'", () => {
    expect(isIdLike(COLS[0], 1000)).toBe(true);
    expect(isIdLike({ name: "id", dtype: "Int64", distinct: 3 }, 1000)).toBe(true);
    expect(isIdLike({ name: "orderId", dtype: "Utf8", distinct: 3 }, 1000)).toBe(true);
    expect(isIdLike(COLS[6], 1000)).toBe(false); // "paid"
    expect(isIdLike({ name: "email", dtype: "Utf8", distinct: 1000 }, 1000)).toBe(true);
    expect(isIdLike(COLS[3], 1000)).toBe(false); // floats can be unique legitimately
  });

  it("local preview excludes target/time column and lists id-like columns with a reason", () => {
    const p = localPreview(COLS, "churned", null, 1000);
    expect(p.server).toBe(false);
    expect(p.detected_task_type).toBe("classification");
    expect(p.candidates).not.toContain("churned");
    expect(p.candidates).not.toContain("customer_id");
    expect(p.leakage_dropped.map((r) => r.column)).toEqual(["customer_id"]);
    expect(p.leakage_dropped[0].reason).toMatch(/identifier/i);
    expect(p.excluded_by_policy).toEqual([]);
  });
});

describe("wizard state machine", () => {
  it("cannot leave the dataset step before a dataset and its columns are loaded", () => {
    let s = run([{ type: "next" }]);
    expect(s.step).toBe("dataset");
    s = run([{ type: "select_dataset", id: "ds1", name: "Customers", rowCount: 1000 }, { type: "next" }]);
    expect(s.step).toBe("dataset");
    expect(stepError(s)).toMatch(/loading/i);
    s = run([{ type: "columns_loaded", datasetId: "ds1", columns: COLS }, { type: "next" }], s);
    expect(s.step).toBe("target");
    expect(s.name).toBe("Customers model");
  });

  it("ignores a stale columns response for a previously selected dataset", () => {
    const s = run([
      { type: "select_dataset", id: "ds1", name: "A", rowCount: 10 },
      { type: "select_dataset", id: "ds2", name: "B", rowCount: 10 },
      { type: "columns_loaded", datasetId: "ds1", columns: COLS },
    ]);
    expect(s.columns).toBeNull();
  });

  it("auto-detects the task from the target and lets the user override it", () => {
    let s = run([{ type: "next" }, { type: "set_target", column: "monthly_spend" }], withDataset());
    expect(s.taskType).toBe("regression");
    expect(s.detectedTask).toBe("regression");
    expect(s.taskOverridden).toBe(false);
    s = wizardReducer(s, { type: "set_task", task: "classification" });
    expect(s.taskType).toBe("classification");
    expect(s.taskOverridden).toBe(true);
    // An override survives choosing another target.
    s = wizardReducer(s, { type: "set_target", column: "tenure_months" });
    expect(s.taskType).toBe("classification");
    expect(s.taskOverridden).toBe(true);
  });

  it("requires a time column for forecasts and clears the target for clustering", () => {
    let s = run([{ type: "next" }, { type: "set_target", column: "monthly_spend" }, { type: "set_task", task: "forecast" }, { type: "next" }], withDataset());
    expect(s.step).toBe("target");
    expect(stepError(s)).toMatch(/time column/i);
    s = run([{ type: "set_time_column", column: "signup_date" }, { type: "next" }], s);
    expect(s.step).toBe("features");
    s = run([{ type: "back" }, { type: "set_task", task: "clustering" }], s);
    expect(s.target).toBeNull();
    expect(s.timeColumn).toBeNull();
    expect(stepError(s)).toBeNull();
  });

  it("applies the preview: excluded and leaked columns are never selectable, the rest are preselected", () => {
    let s = run([{ type: "next" }, { type: "set_target", column: "churned" }, { type: "next" }], withDataset());
    expect(s.step).toBe("features");
    expect(stepError(s)).toMatch(/checking/i);
    s = wizardReducer(s, {
      type: "preview_loaded",
      preview: {
        detected_task_type: "classification",
        candidates: ["region", "tenure_months", "monthly_spend", "churned", "signup_date"],
        excluded_by_policy: [{ column: "region", reason: "Masked for you (mask_hash)." }],
        leakage_dropped: [{ column: "customer_id", reason: "All values unique." }],
        server: true,
      },
    });
    expect(s.features).toEqual(["tenure_months", "monthly_spend", "signup_date"]);
    expect([...blockedColumns(s)].sort()).toEqual(["churned", "customer_id", "region"]);
    expect(wizardReducer(s, { type: "toggle_feature", column: "region" })).toBe(s);
    s = wizardReducer(s, { type: "toggle_feature", column: "signup_date" });
    expect(s.features).toEqual(["tenure_months", "monthly_spend"]);
    s = wizardReducer(s, { type: "set_features", columns: ["region", "monthly_spend", "nope", "monthly_spend"] });
    expect(s.features).toEqual(["monthly_spend"]);
    s = wizardReducer(s, { type: "set_features", columns: [] });
    expect(wizardReducer(s, { type: "next" }).step).toBe("features");
  });

  it("changing the target invalidates the feature preview", () => {
    let s = run([{ type: "next" }, { type: "set_target", column: "churned" }], withDataset());
    s = wizardReducer(s, { type: "preview_loaded", preview: localPreview(COLS, "churned", null, 1000) });
    expect(s.features.length).toBeGreaterThan(0);
    s = wizardReducer(s, { type: "set_target", column: "region" });
    expect(s.preview).toBeNull();
    expect(s.features).toEqual([]);
  });

  it("goto: back is free, forward only through complete steps", () => {
    const s = run([{ type: "next" }], withDataset());
    expect(wizardReducer(s, { type: "goto", step: "review" }).step).toBe("target");
    expect(wizardReducer(s, { type: "goto", step: "dataset" }).step).toBe("dataset");
  });

  it("builds the create request only when every step is complete", () => {
    let s = run([{ type: "next" }, { type: "set_target", column: "churned" }, { type: "next" }], withDataset());
    expect(toCreateRequest(s)).toBeNull();
    s = run(
      [
        { type: "preview_loaded", preview: localPreview(COLS, "churned", null, 1000) },
        { type: "next" },
        { type: "set_name", name: "  Churn  " },
        { type: "set_description", description: "Predict churn" },
      ],
      s,
    );
    expect(s.step).toBe("review");
    expect(toCreateRequest(s)).toEqual({
      name: "Churn",
      description: "Predict churn",
      dataset_id: "ds1",
      target_column: "churned",
      task_type: "classification",
      features: ["region", "tenure_months", "monthly_spend", "signup_date", "paid"],
    });
    expect(toCreateRequest(wizardReducer(s, { type: "set_name", name: "  " }))).toBeNull();
  });

  it("forecast requests carry the time column and never list it as a feature", () => {
    const s = run(
      [
        { type: "next" },
        { type: "set_target", column: "monthly_spend" },
        { type: "set_task", task: "forecast" },
        { type: "set_time_column", column: "signup_date" },
        { type: "next" },
        { type: "preview_loaded", preview: localPreview(COLS, "monthly_spend", "signup_date", 1000) },
        { type: "next" },
      ],
      withDataset(),
    );
    const req = toCreateRequest(s)!;
    expect(req.task_type).toBe("forecast");
    expect(req.time_column).toBe("signup_date");
    expect(req.features).not.toContain("signup_date");
    expect(req.features).not.toContain("monthly_spend");
  });
});
