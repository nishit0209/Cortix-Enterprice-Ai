"use client";

/**
 * Document intelligence completion (API_SPECIFICATION Phase 8): collections, schema extraction with citations,
 * document comparison and table promotion. Responses are normalized here so the screens tolerate field aliases.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { isUnavailableEndpoint } from "@/lib/errors";
import { isObj, listOf, num } from "@/lib/ml/mappers";
import { docKeys } from "./documents";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;
const retry = (n: number, e: unknown) => !isUnavailableEndpoint(e) && n < 1;
const str = (v: unknown): string | null => (typeof v === "string" ? v : typeof v === "number" ? String(v) : null);

export const EXTRACTION_SCHEMAS = [
  { value: "invoice", label: "Invoice" },
  { value: "receipt", label: "Receipt" },
  { value: "resume", label: "Resume" },
];

export interface DocCollection {
  id: string;
  name: string;
  description: string;
  classification: string | null;
  document_count: number | null;
  created_at: string | null;
}

export interface Citation {
  page: number;
  quote: string | null;
}

export interface ExtractedField {
  name: string;
  value: unknown;
  confidence: number | null;
  citations: Citation[];
}

export interface Extraction {
  id: string;
  schema_key: string;
  method: string | null;
  status: string;
  reason: string | null;
  fields: ExtractedField[];
  created_at: string | null;
}

export interface CompareSection {
  title: string;
  change: "added" | "removed" | "changed" | "same";
  a: string | null;
  b: string | null;
  similarity: number | null;
}

export interface CompareResult {
  sections: CompareSection[];
  similarity: number | null;
  summary: string | null;
  summary_mode: string | null;
}

export const extraKeys = {
  collections: (org: string) => ["org", org, "documents", "collections"] as const,
  extractions: (org: string, id: string) => ["org", org, "documents", id, "extractions"] as const,
};

export function mapCollection(raw: unknown): DocCollection {
  const r = isObj(raw) ? raw : {};
  return {
    id: str(r.id) ?? "",
    name: str(r.name) ?? "Collection",
    description: str(r.description) ?? "",
    classification: str(r.classification),
    document_count: num(r.document_count ?? r.documents_count ?? r.count),
    created_at: str(r.created_at),
  };
}

export function mapExtraction(raw: unknown): Extraction {
  const r = isObj(raw) ? raw : {};
  const result = isObj(r.result) ? r.result : isObj(r.fields) ? r.fields : {};
  const corrections = isObj(r.corrections) ? r.corrections : {};
  const cites = isObj(r.field_citations) ? r.field_citations : {};
  const conf = isObj(r.field_confidence) ? r.field_confidence : {};
  const fields: ExtractedField[] = Object.keys(result).map((name) => {
    const c = cites[name];
    const list = Array.isArray(c) ? c : isObj(c) ? [c] : [];
    return {
      name,
      value: name in corrections ? corrections[name] : result[name],
      confidence: num(conf[name]),
      citations: list.filter(isObj).map((x) => ({ page: num(x.page) ?? 1, quote: str(x.quote) ?? str(x.text) })),
    };
  });
  return {
    id: str(r.id) ?? "",
    schema_key: str(r.schema_key) ?? "",
    method: str(r.method),
    status: str(r.status) ?? "needs_review",
    reason: str(r.reason),
    fields,
    created_at: str(r.created_at),
  };
}

const CHANGE = new Set(["added", "removed", "changed", "same"]);

export function mapCompare(raw: unknown): CompareResult {
  const r = isObj(raw) ? raw : {};
  const list = listOf(r, "sections", "diff", "changes");
  const sections: CompareSection[] = list.filter(isObj).map((s, i) => {
    const rawChange = str(s.change) ?? str(s.status) ?? str(s.op) ?? str(s.type) ?? "changed";
    const change = (CHANGE.has(rawChange) ? rawChange : rawChange === "insert" ? "added" : rawChange === "delete" ? "removed" : rawChange === "equal" ? "same" : "changed") as CompareSection["change"];
    return {
      title: str(s.title) ?? str(s.heading) ?? str(s.section) ?? `Section ${i + 1}`,
      change,
      a: str(s.a) ?? str(s.text_a) ?? str(s.left) ?? (change === "removed" ? str(s.text) : null),
      b: str(s.b) ?? str(s.text_b) ?? str(s.right) ?? (change === "added" ? str(s.text) : null),
      similarity: num(s.similarity ?? s.ratio),
    };
  });
  const summary = r.summary;
  return {
    sections,
    similarity: num(r.similarity ?? r.ratio),
    summary: typeof summary === "string" ? summary : isObj(summary) ? str(summary.text) : null,
    summary_mode: str(r.summary_mode) ?? (isObj(summary) ? str(summary.mode) : null),
  };
}

export function useCollections(org: string) {
  return useQuery({ queryKey: extraKeys.collections(org), retry, queryFn: async ({ signal }) => listOf(await api.get<unknown>(`${o(org)}/document-collections`, { signal, query: { limit: 200 } }), "collections").map(mapCollection) });
}

export function useCreateCollection(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { name: string; description?: string; classification?: string }) => mapCollection(await api.post<unknown>(`${o(org)}/document-collections`, body)),
    onSuccess: () => qc.invalidateQueries({ queryKey: extraKeys.collections(org) }),
  });
}

export function useSetDocumentCollection(org: string, documentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (collectionId: string | null) => api.patch<unknown>(`${o(org)}/documents/${documentId}`, { collection_id: collectionId }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: docKeys.list(org) });
      void qc.invalidateQueries({ queryKey: extraKeys.collections(org) });
    },
  });
}

export function useExtractions(org: string, documentId: string, enabled = true) {
  return useQuery({
    queryKey: extraKeys.extractions(org, documentId),
    enabled,
    retry,
    queryFn: async ({ signal }) => listOf(await api.get<unknown>(`${o(org)}/documents/${documentId}/extractions`, { signal }), "extractions").map(mapExtraction),
  });
}

export function useExtract(org: string, documentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (schemaKey: string) => {
      const raw = await api.post<unknown>(`${o(org)}/documents/${documentId}/extract`, { schema_key: schemaKey });
      return mapExtraction(isObj(raw) && isObj(raw.extraction_result) ? raw.extraction_result : raw);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: extraKeys.extractions(org, documentId) }),
  });
}

export function useCompareDocuments(org: string) {
  return useMutation({ mutationFn: async (body: { a: string; b: string; summary?: boolean }) => mapCompare(await api.post<unknown>(`${o(org)}/documents/compare`, body)) });
}

export function usePromoteTable(org: string, documentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ tableId, ...body }: { tableId: string; name: string; classification: string; attest?: boolean }) => {
      const raw = await api.post<unknown>(`${o(org)}/document-tables/${tableId}/promote`, body);
      const r = isObj(raw) ? raw : {};
      const ds = isObj(r.dataset) ? r.dataset : null;
      return { dataset_id: str(r.dataset_id) ?? (ds ? str(ds.id) : null), job_id: str(r.job_id) };
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: docKeys.detail(org, documentId) }),
  });
}
