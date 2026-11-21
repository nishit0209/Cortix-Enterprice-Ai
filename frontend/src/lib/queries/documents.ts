"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fetchAll, unwrapList, uploadWithProgress, type UploadProgress } from "@/lib/api";
import type {
  Classification,
  DocumentAnswer,
  DocumentChunk,
  DocumentEntity,
  DocumentItem,
  DocumentPage,
  DocumentSearchHit,
  DocumentTable,
  Paginated,
} from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export const docKeys = {
  list: (org: string) => ["org", org, "documents"] as const,
  detail: (org: string, id: string) => ["org", org, "documents", id] as const,
  part: (org: string, id: string, part: string) => ["org", org, "documents", id, part] as const,
};

const PROCESSING = new Set(["processing", "uploaded", "queued", "running"]);
export const isDocProcessing = (d: Pick<DocumentItem, "status"> | undefined) => !!d && PROCESSING.has(d.status);

export function useDocuments(org: string) {
  return useQuery({
    queryKey: docKeys.list(org),
    queryFn: async ({ signal }) => unwrapList(await api.get<DocumentItem[] | Paginated<DocumentItem>>(`${o(org)}/documents`, { signal, query: { limit: 200 } })),
    refetchInterval: (q) => ((q.state.data ?? []).some(isDocProcessing) ? 5_000 : false),
  });
}

export function useDocument(org: string, id: string) {
  return useQuery({
    queryKey: docKeys.detail(org, id),
    queryFn: ({ signal }) => api.get<DocumentItem>(`${o(org)}/documents/${id}`, { signal }),
    refetchInterval: (q) => (isDocProcessing(q.state.data) ? 4_000 : false),
  });
}

/** Pages and passages are cursor-paginated (max 200 per page); entities/tables return `{items}`. Follow every cursor. */
function usePart<T>(org: string, id: string, part: string, enabled: boolean) {
  return useQuery({
    queryKey: docKeys.part(org, id, part),
    enabled,
    retry: false,
    queryFn: ({ signal }) => fetchAll<T>(`${o(org)}/documents/${id}/${part}`, { signal, maxItems: 5000 }),
  });
}

export const useDocumentPages = (org: string, id: string, enabled = true) => usePart<DocumentPage>(org, id, "pages", enabled);
export const useDocumentChunks = (org: string, id: string, enabled = true) => usePart<DocumentChunk>(org, id, "chunks", enabled);
export const useDocumentEntities = (org: string, id: string, enabled = true) => usePart<DocumentEntity>(org, id, "entities", enabled);
export const useDocumentTables = (org: string, id: string, enabled = true) => usePart<DocumentTable>(org, id, "tables", enabled);

export interface DocumentUploadResult {
  document?: DocumentItem;
  id?: string;
  job_id?: string;
  events_url?: string;
}

export function uploadDocument(
  org: string,
  file: File,
  meta: { title?: string; classification: Classification; attest?: boolean },
  onProgress?: (p: UploadProgress) => void,
  signal?: AbortSignal,
) {
  const form = new FormData();
  form.append("file", file);
  if (meta.title) form.append("title", meta.title);
  form.append("classification", meta.classification);
  if (meta.attest) form.append("attest", "true");
  return uploadWithProgress<DocumentUploadResult>(`${o(org)}/documents`, form, onProgress, signal);
}

export function useDeleteDocument(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/documents/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: docKeys.list(org) }),
  });
}

interface DocumentSearchResponse {
  results?: DocumentSearchHit[];
  items?: DocumentSearchHit[];
  search_mode?: string | null;
  search_mode_reason?: string | null;
  search_mode_message?: string | null;
  retrieval?: { pending_embeddings?: number | null } | null;
}

export interface DocumentSearchResult {
  hits: DocumentSearchHit[];
  /** `hybrid` / `vector` / `fulltext` (W3). */
  mode: string | null;
  /** Shown when the search fell back to full-text (e.g. embeddings unavailable or still pending). */
  message: string | null;
  pendingEmbeddings: number;
}

/** Normalize the search payload; `message` only when the backend reports a full-text fallback. */
export function normalizeSearch(res: DocumentSearchResponse | DocumentSearchHit[]): DocumentSearchResult {
  if (Array.isArray(res)) return { hits: res, mode: null, message: null, pendingEmbeddings: 0 };
  const mode = res.search_mode ?? null;
  return {
    hits: res.results ?? res.items ?? [],
    mode,
    message: mode === "fulltext" ? (res.search_mode_message ?? null) : null,
    pendingEmbeddings: Number(res.retrieval?.pending_embeddings ?? 0) || 0,
  };
}

/** GET /documents/search?q=&k=&document_ids= (hybrid vector + full-text search, RRF-fused). */
export function useDocumentSearch(org: string) {
  return useMutation({
    mutationFn: async (body: { query: string; limit?: number; document_ids?: string[] }) => {
      const res = await api.get<DocumentSearchResponse | DocumentSearchHit[]>(`${o(org)}/documents/search`, {
        query: { q: body.query, k: body.limit ?? 20, document_ids: body.document_ids?.length ? body.document_ids.join(",") : undefined },
      });
      return normalizeSearch(res);
    },
  });
}

/** POST /documents/ask (JSON). `mode: "extractive"` + `reason` when generation is not allowed or not available. */
export function askDocuments(org: string, body: { question: string; document_ids?: string[] }, signal?: AbortSignal) {
  return api.post<DocumentAnswer>(`${o(org)}/documents/ask`, body, { signal });
}

/* ---------------- highlighting ---------------- */

export interface TextSegment {
  text: string;
  match: boolean;
}

const STOP = new Set(["the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are", "what", "which", "how", "with", "by"]);

/** Split text into segments marking whole-word, case-insensitive matches of the query terms (no HTML involved). */
export function highlightTerms(text: string, query: string): TextSegment[] {
  const terms = Array.from(new Set(query.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter((t) => t.length > 1 && !STOP.has(t))));
  if (!terms.length || !text) return [{ text, match: false }];
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length);
  const re = new RegExp(`(?<![\\p{L}\\p{N}])(${escaped.join("|")})(?![\\p{L}\\p{N}])`, "giu");
  const out: TextSegment[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const i = m.index ?? 0;
    if (i > last) out.push({ text: text.slice(last, i), match: false });
    out.push({ text: m[0], match: true });
    last = i + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), match: false });
  return out;
}

/** A window of text around the first match (for long chunks in result lists). */
export function snippet(text: string, query: string, radius = 180): string {
  const segs = highlightTerms(text, query);
  const firstIdx = segs.findIndex((s) => s.match);
  if (firstIdx < 0 || text.length <= radius * 2) return text.length > radius * 2 ? `${text.slice(0, radius * 2)}…` : text;
  const offset = segs.slice(0, firstIdx).reduce((a, s) => a + s.text.length, 0);
  const start = Math.max(0, offset - radius);
  const end = Math.min(text.length, offset + radius);
  return `${start > 0 ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`;
}
