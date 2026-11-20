"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrapList, uploadWithProgress, type UploadProgress } from "@/lib/api";
import type {
  Classification,
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

const PROCESSING = new Set(["uploaded", "extracting", "ocr", "chunking", "embedding", "processing", "queued", "running"]);
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

function usePart<T>(org: string, id: string, part: string, enabled: boolean) {
  return useQuery({
    queryKey: docKeys.part(org, id, part),
    enabled,
    retry: false,
    queryFn: async ({ signal }) => unwrapList(await api.get<T[] | Paginated<T>>(`${o(org)}/documents/${id}/${part}`, { signal, query: { limit: 500 } })),
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

export function useDocumentSearch(org: string) {
  return useMutation({
    mutationFn: async (body: { query: string; limit?: number; document_ids?: string[] }) => {
      const res = await api.post<{ items?: DocumentSearchHit[]; results?: DocumentSearchHit[] } | DocumentSearchHit[]>(`${o(org)}/documents/search`, { limit: 20, ...body });
      if (Array.isArray(res)) return res;
      return res.items ?? res.results ?? [];
    },
  });
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
