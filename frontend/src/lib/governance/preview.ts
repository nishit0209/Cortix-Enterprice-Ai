/**
 * Policy-preview interpretation (POST /datasets/{id}/policy-preview -> `policy_to_dict`). Pure; unit-tested.
 *
 * `allowed_columns` lists the columns a user may reference per logical table (masked ones included);
 * `column_masks` says how a column is masked; anything in the dataset but not allowed is denied.
 */

import { summarizePredicate, type WireCondition } from "./conditions";
import type { PolicyPreview } from "@/lib/queries/governance";

export type ColumnAccess = { column: string; status: "visible" | "masked" | "denied"; mask: string | null };

const MASK_LABELS: Record<string, string> = { mask_null: "empty", mask_hash: "hashed", mask_partial: "partial" };

export function previewColumns(datasetColumns: string[], preview: PolicyPreview | null | undefined): ColumnAccess[] {
  if (!preview?.access || !preview.policy) return datasetColumns.map((column) => ({ column, status: "denied", mask: null }));
  const allowed = new Set(Object.values(preview.policy.allowed_columns ?? {}).flat());
  const masks = new Map<string, string>();
  for (const m of preview.policy.column_masks ?? []) {
    const label = MASK_LABELS[m.kind] ?? m.kind;
    masks.set(m.column, m.kind === "mask_partial" && m.keep_last !== null ? `${label}, last ${m.keep_last}` : label);
  }
  const names = datasetColumns.length ? datasetColumns : Array.from(allowed);
  return names.map((column) => {
    if (!allowed.has(column)) return { column, status: "denied", mask: null };
    const mask = masks.get(column) ?? null;
    return { column, status: mask ? "masked" : "visible", mask };
  });
}

/** Row predicate summary per logical table ("All rows" when unrestricted). */
export function previewRowSummary(preview: PolicyPreview | null | undefined): Array<{ table: string; summary: string }> {
  const preds = preview?.policy?.row_predicates ?? {};
  const tables = Object.keys(preview?.policy?.tables ?? {});
  const keys = Array.from(new Set([...tables, ...Object.keys(preds)]));
  if (!keys.length) return [{ table: "", summary: "All rows" }];
  return keys.map((table) => ({ table, summary: summarizePredicate((preds[table] ?? []) as WireCondition[]) }));
}
