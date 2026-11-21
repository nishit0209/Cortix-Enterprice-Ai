import { describe, expect, it } from "vitest";
import { mapCollection, mapCompare, mapExtraction } from "./document-extras";
import { searchModeMessage } from "./products";

describe("document extras mappers", () => {
  it("maps an extraction with citations, confidence and reviewer corrections", () => {
    const x = mapExtraction({
      id: "e1",
      schema_key: "invoice",
      method: "local",
      status: "needs_review",
      reason: "policy_synthetic_only",
      result: { invoice_number: "INV-7", total: 1180, due_date: null },
      field_citations: { invoice_number: [{ page: 1, start: 10, end: 15, quote: "Invoice No: INV-7" }], total: { page: 2 }, due_date: null },
      field_confidence: { invoice_number: 0.95, total: 0.8 },
      corrections: { total: 1200 },
    });
    expect(x.fields).toEqual([
      { name: "invoice_number", value: "INV-7", confidence: 0.95, citations: [{ page: 1, quote: "Invoice No: INV-7" }] },
      { name: "total", value: 1200, confidence: 0.8, citations: [{ page: 2, quote: null }] },
      { name: "due_date", value: null, confidence: null, citations: [] },
    ]);
    expect(x.reason).toBe("policy_synthetic_only");
  });

  it("maps compare sections from several dialects", () => {
    const r = mapCompare({
      similarity: 0.71,
      sections: [
        { heading: "Payment terms", status: "changed", text_a: "30 days", text_b: "45 days", ratio: 0.6 },
        { title: "Annex B", change: "added", text: "New annex" },
        { section: "Scope", op: "equal", a: "Same", b: "Same" },
      ],
      summary: { text: "Payment terms extended.", mode: "extractive" },
    });
    expect(r.similarity).toBe(0.71);
    expect(r.sections).toEqual([
      { title: "Payment terms", change: "changed", a: "30 days", b: "45 days", similarity: 0.6 },
      { title: "Annex B", change: "added", a: null, b: "New annex", similarity: null },
      { title: "Scope", change: "same", a: "Same", b: "Same", similarity: null },
    ]);
    expect(r.summary).toBe("Payment terms extended.");
    expect(r.summary_mode).toBe("extractive");
    expect(mapCompare({ diff: [{ op: "delete", text: "gone" }] }).sections[0]).toMatchObject({ change: "removed", a: "gone", b: null });
  });

  it("maps collections with a document count alias", () => {
    expect(mapCollection({ id: "c", name: "Invoices FY26", documents_count: 4 })).toMatchObject({ name: "Invoices FY26", document_count: 4, description: "" });
  });
});

describe("product search mode copy", () => {
  it("explains a keyword fallback and says nothing for semantic results", () => {
    expect(searchModeMessage({ search_mode: "semantic", reason: null })).toBeNull();
    expect(searchModeMessage({ search_mode: "text", reason: "model_unavailable" })).toMatch(/embedding model/);
    expect(searchModeMessage({ search_mode: "text", reason: "not_indexed" })).toMatch(/run normalization/);
  });
});
