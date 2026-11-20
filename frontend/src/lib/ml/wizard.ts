/**
 * New-project wizard state machine: dataset -> target + task type -> features -> review.
 *
 * Pure reducer (unit-tested in `wizard.test.ts`). The component feeds it API results (dataset columns, the server
 * feature preview) and user choices; the reducer owns step transitions and their guards so the UI can never
 * submit an inconsistent project (e.g. the target listed as a feature, or a forecast without a time column).
 */

import type { ColumnReason, CreateProjectRequest, FeaturePreview, TaskType } from "./types";

export type WizardStep = "dataset" | "target" | "features" | "review";
export const WIZARD_STEPS: WizardStep[] = ["dataset", "target", "features", "review"];

export interface WizardColumn {
  name: string;
  dtype: string;
  distinct: number | null;
  is_pii?: boolean;
}

export interface WizardState {
  step: WizardStep;
  datasetId: string | null;
  datasetName: string | null;
  rowCount: number | null;
  columns: WizardColumn[] | null;
  target: string | null;
  taskType: TaskType | null;
  detectedTask: TaskType | null;
  taskOverridden: boolean;
  timeColumn: string | null;
  preview: FeaturePreview | null;
  features: string[];
  name: string;
  description: string;
}

export const initialWizard: WizardState = {
  step: "dataset",
  datasetId: null,
  datasetName: null,
  rowCount: null,
  columns: null,
  target: null,
  taskType: null,
  detectedTask: null,
  taskOverridden: false,
  timeColumn: null,
  preview: null,
  features: [],
  name: "",
  description: "",
};

export type WizardAction =
  | { type: "select_dataset"; id: string; name: string; rowCount: number | null }
  | { type: "columns_loaded"; datasetId: string; columns: WizardColumn[] }
  | { type: "set_target"; column: string | null }
  | { type: "set_task"; task: TaskType }
  | { type: "set_time_column"; column: string | null }
  | { type: "preview_loaded"; preview: FeaturePreview }
  | { type: "toggle_feature"; column: string }
  | { type: "set_features"; columns: string[] }
  | { type: "set_name"; name: string }
  | { type: "set_description"; description: string }
  | { type: "next" }
  | { type: "back" }
  | { type: "goto"; step: WizardStep };

const isNumeric = (dtype: string) => /int|float|double|decimal|numeric|number/i.test(dtype);
const isFloat = (dtype: string) => /float|double|decimal|numeric/i.test(dtype);
export const isTemporal = (dtype: string) => /date|time/i.test(dtype);

/**
 * Auto-detected task for a target (mirrors the backend rule): a numeric target with many distinct values is a
 * regression; anything else (categorical, boolean, few numeric levels) is a classification.
 */
export function detectTask(col: WizardColumn | undefined, rowCount: number | null): TaskType | null {
  if (!col) return null;
  if (!isNumeric(col.dtype)) return "classification";
  const distinct = col.distinct;
  if (distinct === null) return isFloat(col.dtype) ? "regression" : "classification";
  if (distinct <= 20) return "classification";
  if (rowCount && rowCount > 0 && distinct / rowCount < 0.05 && !isFloat(col.dtype)) return "classification";
  return "regression";
}

/** ID-like column: named `id`, `*_id`, `*Id`, or (non-float) with a distinct value per row. */
export function isIdLike(col: WizardColumn, rowCount: number | null): boolean {
  if (/(^|_)id$/i.test(col.name) || /[a-z]Id$/.test(col.name)) return true;
  return !isFloat(col.dtype) && !isTemporal(col.dtype) && rowCount !== null && rowCount > 1 && col.distinct === rowCount;
}

/**
 * Local fallback for the feature preview when the server has no preview route yet: leakage-style ID columns
 * are listed with a reason; policy exclusions are NOT guessed (the server applies them on create).
 */
export function localPreview(columns: WizardColumn[], target: string | null, timeColumn: string | null, rowCount: number | null): FeaturePreview {
  const leakage: ColumnReason[] = [];
  const candidates: string[] = [];
  for (const c of columns) {
    if (c.name === target || c.name === timeColumn) continue;
    if (isIdLike(c, rowCount)) leakage.push({ column: c.name, reason: "Identifier-like column (unique per row or named like an id)." });
    else candidates.push(c.name);
  }
  const t = columns.find((c) => c.name === target);
  return { detected_task_type: detectTask(t, rowCount), candidates, excluded_by_policy: [], leakage_dropped: leakage, server: false };
}

/** Columns that may never be features in the current state. */
export function blockedColumns(s: WizardState): Set<string> {
  const out = new Set<string>();
  if (s.target) out.add(s.target);
  if (s.taskType === "forecast" && s.timeColumn) out.add(s.timeColumn);
  for (const r of s.preview?.excluded_by_policy ?? []) out.add(r.column);
  for (const r of s.preview?.leakage_dropped ?? []) out.add(r.column);
  return out;
}

/** Why the current step can't advance (null = it can). */
export function stepError(s: WizardState, step: WizardStep = s.step): string | null {
  switch (step) {
    case "dataset":
      if (!s.datasetId) return "Choose a dataset.";
      if (!s.columns) return "Loading the dataset's columns…";
      if (s.columns.length < 2) return "The dataset needs at least two columns.";
      return null;
    case "target":
      if (!s.taskType) return "Choose a task type.";
      if (s.taskType !== "clustering" && !s.target) return "Choose the column to predict.";
      if (s.taskType === "forecast" && !s.timeColumn) return "Forecasts need a time column.";
      if (s.taskType === "forecast" && s.timeColumn === s.target) return "The time column can't be the target.";
      return null;
    case "features":
      if (!s.preview) return "Checking which columns can be used…";
      if (s.features.length === 0) return "Select at least one feature.";
      return null;
    case "review":
      if (!s.name.trim()) return "Name the project.";
      if (s.name.trim().length > 120) return "Use at most 120 characters.";
      return null;
  }
}

function resetFrom(s: WizardState, step: "target" | "features"): WizardState {
  if (step === "target") return { ...s, target: null, taskType: null, detectedTask: null, taskOverridden: false, timeColumn: null, preview: null, features: [] };
  return { ...s, preview: null, features: [] };
}

export function wizardReducer(s: WizardState, a: WizardAction): WizardState {
  switch (a.type) {
    case "select_dataset": {
      if (a.id === s.datasetId) return s;
      const name = s.name && s.name !== defaultName(s.datasetName) ? s.name : defaultName(a.name);
      return { ...resetFrom({ ...initialWizard, step: s.step }, "target"), datasetId: a.id, datasetName: a.name, rowCount: a.rowCount, name };
    }
    case "columns_loaded":
      if (a.datasetId !== s.datasetId) return s; // stale response for a previously selected dataset
      return { ...s, columns: a.columns };
    case "set_target": {
      if (a.column === s.target) return s;
      const col = s.columns?.find((c) => c.name === a.column);
      const detected = a.column ? detectTask(col, s.rowCount) : null;
      const keepTask = s.taskOverridden && s.taskType && s.taskType !== "clustering";
      return {
        ...resetFrom(s, "features"),
        target: a.column,
        detectedTask: detected,
        taskType: keepTask ? s.taskType : (detected ?? (s.taskType === "clustering" ? "clustering" : null)),
        taskOverridden: keepTask ? s.taskOverridden : false,
        timeColumn: s.timeColumn === a.column ? null : s.timeColumn,
      };
    }
    case "set_task": {
      const next = resetFrom(s, "features");
      return {
        ...next,
        taskType: a.task,
        taskOverridden: a.task !== s.detectedTask,
        target: a.task === "clustering" ? null : s.target,
        timeColumn: a.task === "forecast" ? s.timeColumn : null,
      };
    }
    case "set_time_column":
      return { ...resetFrom(s, "features"), timeColumn: a.column };
    case "preview_loaded": {
      const next = { ...s, preview: a.preview };
      const blocked = blockedColumns(next);
      return { ...next, features: a.preview.candidates.filter((c) => !blocked.has(c)) };
    }
    case "toggle_feature": {
      if (blockedColumns(s).has(a.column)) return s;
      const has = s.features.includes(a.column);
      return { ...s, features: has ? s.features.filter((f) => f !== a.column) : [...s.features, a.column] };
    }
    case "set_features": {
      const blocked = blockedColumns(s);
      const allowed = new Set(s.preview?.candidates ?? []);
      return { ...s, features: Array.from(new Set(a.columns)).filter((c) => !blocked.has(c) && allowed.has(c)) };
    }
    case "set_name":
      return { ...s, name: a.name };
    case "set_description":
      return { ...s, description: a.description };
    case "next": {
      if (stepError(s) !== null) return s;
      const i = WIZARD_STEPS.indexOf(s.step);
      return i < WIZARD_STEPS.length - 1 ? { ...s, step: WIZARD_STEPS[i + 1] } : s;
    }
    case "back": {
      const i = WIZARD_STEPS.indexOf(s.step);
      return i > 0 ? { ...s, step: WIZARD_STEPS[i - 1] } : s;
    }
    case "goto": {
      // Going back is always allowed; going forward only through steps that are complete.
      const target = WIZARD_STEPS.indexOf(a.step);
      const current = WIZARD_STEPS.indexOf(s.step);
      if (target <= current) return { ...s, step: a.step };
      for (let i = 0; i < target; i++) if (stepError(s, WIZARD_STEPS[i]) !== null) return s;
      return { ...s, step: a.step };
    }
  }
}

function defaultName(datasetName: string | null): string {
  return datasetName ? `${datasetName} model` : "";
}

/** The POST /ml/projects body, or null while the wizard is incomplete. */
export function toCreateRequest(s: WizardState): CreateProjectRequest | null {
  if (WIZARD_STEPS.some((step) => stepError(s, step) !== null)) return null;
  return {
    name: s.name.trim(),
    ...(s.description.trim() ? { description: s.description.trim() } : {}),
    dataset_id: s.datasetId!,
    target_column: s.taskType === "clustering" ? null : s.target,
    task_type: s.taskType!,
    ...(s.taskType === "forecast" ? { time_column: s.timeColumn } : {}),
    features: [...s.features],
  };
}
