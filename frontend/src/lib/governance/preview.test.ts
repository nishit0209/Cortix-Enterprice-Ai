import { describe, expect, it } from "vitest";
import type { PolicyPreview } from "@/lib/queries/governance";
import { previewColumns, previewRowSummary } from "./preview";

const policy = (over: Partial<NonNullable<PolicyPreview["policy"]>> = {}): PolicyPreview => ({
  access: true,
  policy: {
    tables: { sales: "ds_sales_v3" },
    allowed_columns: { sales: ["region", "amount", "email", "card"] },
    column_masks: [
      { table: "sales", column: "email", kind: "mask_hash", keep_last: null },
      { table: "sales", column: "card", kind: "mask_partial", keep_last: 4 },
    ],
    row_predicates: { sales: [{ col: "region", op: "in", value: "$user.regions" }] },
    row_cap: 100000,
    byte_cap: null,
    timeout_s: 30,
    fingerprint: "abc",
    ...over,
  },
});

describe("policy preview interpretation", () => {
  it("classifies dataset columns as visible, masked (with mask label) or denied", () => {
    const cols = previewColumns(["region", "amount", "email", "card", "ssn"], policy());
    expect(cols).toEqual([
      { column: "region", status: "visible", mask: null },
      { column: "amount", status: "visible", mask: null },
      { column: "email", status: "masked", mask: "hashed" },
      { column: "card", status: "masked", mask: "partial, last 4" },
      { column: "ssn", status: "denied", mask: null },
    ]);
  });

  it("denies everything without access", () => {
    expect(previewColumns(["a", "b"], { access: false, policy: null }).map((c) => c.status)).toEqual(["denied", "denied"]);
  });

  it("falls back to the allowed columns when the dataset columns are not loaded", () => {
    expect(previewColumns([], policy()).map((c) => c.column)).toEqual(["region", "amount", "email", "card"]);
  });

  it("summarizes row predicates per table and says All rows when unrestricted", () => {
    expect(previewRowSummary(policy())).toEqual([{ table: "sales", summary: "region is one of $user.regions" }]);
    expect(previewRowSummary(policy({ row_predicates: {} }))).toEqual([{ table: "sales", summary: "All rows" }]);
    expect(previewRowSummary(null)).toEqual([{ table: "", summary: "All rows" }]);
  });
});
