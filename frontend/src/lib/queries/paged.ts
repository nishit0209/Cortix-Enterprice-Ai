"use client";

/**
 * Cursor paging over the standard list envelope `{items, next_cursor, previous_cursor}` (CONTRACT "More W3 as-built
 * details"). Keeps a stack of cursors so the UI can go forward and back; resets when the filters change.
 */

import * as React from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, cursorFrom, type Query } from "@/lib/api";
import { isUnavailableEndpoint } from "@/lib/errors";

export interface CursorPage<T> {
  items: T[];
  next: string | null;
}

export function toCursorPage<T>(raw: unknown, map: (r: unknown) => T): CursorPage<T> {
  if (Array.isArray(raw)) return { items: raw.map(map), next: null };
  const o = (raw && typeof raw === "object" ? raw : {}) as { items?: unknown; next_cursor?: unknown };
  const items = Array.isArray(o.items) ? o.items.map(map) : [];
  return { items, next: cursorFrom(typeof o.next_cursor === "string" ? o.next_cursor : null) };
}

export function useCursorList<T>(
  key: readonly unknown[],
  path: string,
  { query = {}, limit = 50, enabled = true, map = (r: unknown) => r as T }: { query?: Query; limit?: number; enabled?: boolean; map?: (r: unknown) => T } = {},
) {
  const filterKey = JSON.stringify(query);
  const [stack, setStack] = React.useState<string[]>([]);
  const [lastFilter, setLastFilter] = React.useState(filterKey);
  if (lastFilter !== filterKey) {
    setLastFilter(filterKey);
    setStack([]);
  }
  const cursor = stack.length ? stack[stack.length - 1] : null;
  const q = useQuery({
    queryKey: [...key, "page", filterKey, cursor, limit],
    enabled,
    placeholderData: keepPreviousData,
    retry: (n, e) => !isUnavailableEndpoint(e) && n < 1,
    queryFn: async ({ signal }) => toCursorPage<T>(await api.get<unknown>(path, { signal, query: { ...query, limit, cursor: cursor ?? undefined } }), map),
  });
  const next = q.data?.next ?? null;
  return {
    ...q,
    items: q.data?.items,
    page: stack.length + 1,
    hasNext: !!next,
    hasPrev: stack.length > 0,
    goNext: () => next && setStack((s) => [...s, next]),
    goPrev: () => setStack((s) => s.slice(0, -1)),
    reset: () => setStack([]),
  };
}
