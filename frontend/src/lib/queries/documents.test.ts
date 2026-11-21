import { describe, expect, it } from "vitest";
import { normalizeSearch } from "./documents";

const hit = { chunk_id: "c", document_id: "d", page: 1, text: "x" };

describe("normalizeSearch", () => {
  it("accepts a bare list", () => expect(normalizeSearch([hit])).toEqual({ hits: [hit], mode: null, message: null, pendingEmbeddings: 0 }));
  it("shows the message only for the full-text fallback", () => {
    const fallback = normalizeSearch({ results: [hit], search_mode: "fulltext", search_mode_message: "Keyword search only.", retrieval: { pending_embeddings: 3 } });
    expect(fallback).toEqual({ hits: [hit], mode: "fulltext", message: "Keyword search only.", pendingEmbeddings: 3 });
    expect(normalizeSearch({ results: [], search_mode: "hybrid", search_mode_message: "ignored" }).message).toBeNull();
  });
});
