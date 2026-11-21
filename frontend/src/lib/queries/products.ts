"use client";

/** Product 360 (API_SPECIFICATION Phase 7; as built in `apps/products/views.py`). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fetchAll, type Query } from "@/lib/api";
import { isUnavailableEndpoint } from "@/lib/errors";
import type { JobAccepted } from "./customers";
import { useCursorList } from "./paged";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}/products`;
const retry = (n: number, e: unknown) => !isUnavailableEndpoint(e) && n < 1;

export interface ProductSource {
  id: string;
  name: string;
  source_system: string;
  dataset_id: string;
  mapping: Record<string, string | string[]>;
  priority: number;
  status: string;
  last_error: string | null;
  record_count: number;
  last_synced_at: string | null;
  created_at: string;
}

export interface ProductRun {
  id: string;
  kind: string;
  status: string;
  job_id: string | null;
  options: Record<string, unknown>;
  stats: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface ProductMaster {
  id: string;
  sku: string | null;
  name: string;
  brand: string | null;
  category: string | null;
  size: string | null;
  price: number | null;
  record_count: number;
  sources: string[];
  quality: number | null;
  updated_at: string;
  score?: number;
}

export interface ProductRecord {
  id: string;
  source_system?: string;
  source_key?: string;
  attributes?: Record<string, unknown>;
  normalized?: Record<string, unknown>;
  master_id?: string | null;
}

export interface ProductMatch {
  id: string;
  score: number;
  status: string;
  features: Record<string, unknown>;
  record_a: ProductRecord;
  record_b: ProductRecord;
  note: string;
  decided_at: string | null;
  created_at: string;
}

export interface ProductCorrection {
  id: string;
  master_id: string;
  field: string;
  old_value: unknown;
  new_value: unknown;
  reason: string;
  confidence: number | null;
  origin: string;
  status: string;
  decided_at: string | null;
  applied_at: string | null;
  note: string;
  created_at: string;
}

export interface ProductAttribute {
  key: string;
  value: unknown;
  origin: string;
}

export interface ProductDetail extends ProductMaster {
  values: Record<string, unknown>;
  overrides: Record<string, unknown>;
  quality_detail: { completeness: number; validity: number; consistency: number; overall: number; issues: unknown } | null;
  attributes: ProductAttribute[];
  records: ProductRecord[];
  matches: ProductMatch[];
  corrections: ProductCorrection[];
}

export interface ProductSearchResult {
  query: string;
  search_mode: "semantic" | "text";
  reason: string | null;
  items: ProductMaster[];
}

export const productKeys = {
  all: (org: string) => ["org", org, "products"] as const,
  list: (org: string) => ["org", org, "products", "list"] as const,
  detail: (org: string, id: string) => ["org", org, "products", "detail", id] as const,
  sources: (org: string) => ["org", org, "products", "sources"] as const,
  runs: (org: string) => ["org", org, "products", "runs"] as const,
  matches: (org: string) => ["org", org, "products", "matches"] as const,
  corrections: (org: string) => ["org", org, "products", "corrections"] as const,
  search: (org: string, q: string, brand: string, category: string) => ["org", org, "products", "search", q, brand, category] as const,
};

export const PRODUCT_FIELDS = ["sku", "name", "brand", "category", "price", "pack_size", "unit", "external_id"] as const;
export const CORRECTABLE_FIELDS = ["name", "brand", "category", "size", "price", "sku"] as const;

export function useProducts(org: string, query: Query) {
  return useCursorList<ProductMaster>(productKeys.list(org), o(org), { query });
}

export function useProduct(org: string, id: string) {
  return useQuery({ queryKey: productKeys.detail(org, id), retry, queryFn: ({ signal }) => api.get<ProductDetail>(`${o(org)}/${id}`, { signal }) });
}

export function useProductSources(org: string) {
  return useQuery({ queryKey: productKeys.sources(org), retry, queryFn: ({ signal }) => fetchAll<ProductSource>(`${o(org)}/sources`, { signal }) });
}

export function useProductRuns(org: string) {
  return useCursorList<ProductRun>(productKeys.runs(org), `${o(org)}/runs`, { limit: 20 });
}

export function useProductMatches(org: string, status: string, enabled = true) {
  return useCursorList<ProductMatch>(productKeys.matches(org), `${o(org)}/matches`, { query: { status }, limit: 20, enabled });
}

export function useProductCorrections(org: string, status: string, enabled = true) {
  return useCursorList<ProductCorrection>(productKeys.corrections(org), `${o(org)}/corrections`, { query: { status }, limit: 25, enabled });
}

export function useProductSearch(org: string, q: string, brand = "", category = "") {
  return useQuery({
    queryKey: productKeys.search(org, q, brand, category),
    enabled: q.trim().length > 0,
    retry,
    queryFn: ({ signal }) =>
      api.post<ProductSearchResult>(`${o(org)}/search`, { query: q.trim(), k: 30, brand: brand || undefined, category: category || undefined }, { signal }),
  });
}

function useInvalidate(org: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: productKeys.all(org) });
}

export function useCreateProductSource(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: (body: { dataset_id: string; mapping: Record<string, string | string[]>; name?: string; priority?: number }) => api.post<ProductSource>(`${o(org)}/sources`, body),
    onSuccess: inv,
  });
}

export function useDeleteProductSource(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (id: string) => api.delete(`${o(org)}/sources/${id}`), onSuccess: inv });
}

export function useStartProductRun(org: string) {
  const inv = useInvalidate(org);
  return useMutation({ mutationFn: (kind: "normalize" | "enrich") => api.post<JobAccepted>(`${o(org)}/${kind}`, {}), onSuccess: inv });
}

export function useDecideProductMatch(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: ({ id, decision, note }: { id: string; decision: "confirm" | "reject"; note?: string }) =>
      api.post<ProductMatch>(`${o(org)}/matches/${id}/decision`, { decision, note: note || undefined }),
    onSuccess: inv,
  });
}

export function useDecideCorrection(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: ({ id, decision, note }: { id: string; decision: "approve" | "reject"; note?: string }) =>
      api.post<ProductCorrection>(`${o(org)}/corrections/${id}/decision`, { decision, note: note || undefined }),
    onSuccess: inv,
  });
}

export function useProposeCorrection(org: string) {
  const inv = useInvalidate(org);
  return useMutation({
    mutationFn: (body: { master_id: string; field: string; value: unknown; reason?: string }) => api.post<ProductCorrection>(`${o(org)}/corrections`, body),
    onSuccess: inv,
  });
}

/** Copy for the `reason` of a text-mode search (`apps/products/services.search`). */
export function searchModeMessage(res: Pick<ProductSearchResult, "search_mode" | "reason">): string | null {
  if (res.search_mode === "semantic") return null;
  if (res.reason === "model_unavailable") return "Semantic search is unavailable (the local embedding model isn't installed), so these are keyword matches.";
  if (res.reason === "not_indexed") return "Products aren't indexed for semantic search yet (run normalization), so these are keyword matches.";
  return "Showing keyword matches.";
}
