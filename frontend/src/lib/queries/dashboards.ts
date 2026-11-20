"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrapList } from "@/lib/api";
import { layoutPatch, type GridItem } from "@/lib/dashboards/layout";
import type {
  CreateWidgetRequest,
  Dashboard,
  DashboardSummary,
  DashboardWidget,
  Paginated,
  SavedQuery,
  WidgetData,
  WidgetLayout,
} from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export const dashKeys = {
  list: (org: string) => ["org", org, "dashboards"] as const,
  detail: (org: string, id: string) => ["org", org, "dashboards", id] as const,
  widgetData: (org: string, id: string, wid: string) => ["org", org, "dashboards", id, "widgets", wid, "data"] as const,
  savedQueries: (org: string) => ["org", org, "saved-queries"] as const,
};

/** The org home shows the caller's default dashboard (or the first one they can see). */
export function pickDefault(list: DashboardSummary[]): DashboardSummary | null {
  return list.find((d) => d.is_default) ?? null;
}

export function useDashboards(org: string, enabled = true) {
  return useQuery({
    queryKey: dashKeys.list(org),
    enabled,
    retry: 1,
    queryFn: async ({ signal }) =>
      unwrapList(await api.get<DashboardSummary[] | Paginated<DashboardSummary>>(`${o(org)}/dashboards`, { signal, query: { limit: 200 } })),
  });
}

export function useDashboard(org: string, id: string | null) {
  return useQuery({
    queryKey: dashKeys.detail(org, id ?? ""),
    enabled: !!id,
    retry: 1,
    queryFn: async ({ signal }) => {
      const d = await api.get<Dashboard>(`${o(org)}/dashboards/${id}`, { signal });
      return { ...d, widgets: d.widgets ?? [] };
    },
  });
}

export function useCreateDashboard(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { title: string; description?: string; is_default?: boolean; is_shared?: boolean }) =>
      api.post<Dashboard>(`${o(org)}/dashboards`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: dashKeys.list(org) }),
  });
}

export interface DashboardPatch {
  title?: string;
  description?: string;
  is_default?: boolean;
  is_shared?: boolean;
  layout?: GridItem[];
}

export function useUpdateDashboard(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: DashboardPatch & { id: string }) => api.patch<Dashboard>(`${o(org)}/dashboards/${id}`, body),
    onSuccess: (d, vars) => {
      if (d && typeof d === "object" && Array.isArray((d as Dashboard).widgets)) qc.setQueryData(dashKeys.detail(org, vars.id), d);
      else void qc.invalidateQueries({ queryKey: dashKeys.detail(org, vars.id) });
      void qc.invalidateQueries({ queryKey: dashKeys.list(org) });
    },
  });
}

/**
 * Persist widget positions. The cache is updated optimistically so the grid never jumps back; on error the
 * previous layout is restored.
 */
export function useSaveLayout(org: string, id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (items: GridItem[]) => api.patch<Dashboard>(`${o(org)}/dashboards/${id}`, layoutPatch(items)),
    onMutate: async (items) => {
      await qc.cancelQueries({ queryKey: dashKeys.detail(org, id) });
      const prev = qc.getQueryData<Dashboard>(dashKeys.detail(org, id));
      if (prev) {
        const pos = new Map(items.map((i) => [i.id, i]));
        qc.setQueryData<Dashboard>(dashKeys.detail(org, id), {
          ...prev,
          widgets: prev.widgets.map((w) => {
            const p = pos.get(w.id);
            return p ? { ...w, layout: { x: p.x, y: p.y, w: p.w, h: p.h } } : w;
          }),
        });
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(dashKeys.detail(org, id), ctx.prev);
    },
  });
}

export function useDeleteDashboard(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/dashboards/${id}`),
    onSuccess: (_d, id) => {
      qc.removeQueries({ queryKey: dashKeys.detail(org, id) });
      void qc.invalidateQueries({ queryKey: dashKeys.list(org) });
    },
  });
}

/* ---------------- widgets ---------------- */

export function useCreateWidget(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ dashboardId, ...body }: CreateWidgetRequest & { dashboardId: string }) =>
      api.post<DashboardWidget>(`${o(org)}/dashboards/${dashboardId}/widgets`, body),
    onSuccess: (_w, vars) => {
      void qc.invalidateQueries({ queryKey: dashKeys.detail(org, vars.dashboardId) });
      void qc.invalidateQueries({ queryKey: dashKeys.list(org) });
      void qc.invalidateQueries({ queryKey: dashKeys.savedQueries(org) });
    },
  });
}

export function useUpdateWidget(org: string, dashboardId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ wid, ...body }: { wid: string; title?: string; text?: string; chart?: DashboardWidget["chart"]; layout?: WidgetLayout }) =>
      api.patch<DashboardWidget>(`${o(org)}/dashboards/${dashboardId}/widgets/${wid}`, body),
    onSuccess: (w, vars) => {
      qc.setQueryData<Dashboard>(dashKeys.detail(org, dashboardId), (prev) =>
        prev ? { ...prev, widgets: prev.widgets.map((x) => (x.id === vars.wid ? { ...x, ...(w && typeof w === "object" ? w : vars) } : x)) } : prev,
      );
      if (vars.chart !== undefined) void qc.invalidateQueries({ queryKey: dashKeys.widgetData(org, dashboardId, vars.wid) });
    },
  });
}

export function useDeleteWidget(org: string, dashboardId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (wid: string) => api.delete(`${o(org)}/dashboards/${dashboardId}/widgets/${wid}`),
    onSuccess: (_d, wid) => {
      qc.setQueryData<Dashboard>(dashKeys.detail(org, dashboardId), (prev) => (prev ? { ...prev, widgets: prev.widgets.filter((w) => w.id !== wid) } : prev));
      qc.removeQueries({ queryKey: dashKeys.widgetData(org, dashboardId, wid) });
      void qc.invalidateQueries({ queryKey: dashKeys.list(org) });
    },
  });
}

/**
 * GET /dashboards/{id}/widgets/{wid}/data: the backend re-runs the saved query under the VIEWER's policy every
 * time (never cached rows), so this is fetched on mount and on demand; `staleTime` only avoids refetch storms.
 */
export function useWidgetData(org: string, dashboardId: string, widget: Pick<DashboardWidget, "id" | "kind">, enabled = true) {
  return useQuery({
    queryKey: dashKeys.widgetData(org, dashboardId, widget.id),
    enabled: enabled && widget.kind !== "text",
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    queryFn: ({ signal }) => api.get<WidgetData>(`${o(org)}/dashboards/${dashboardId}/widgets/${widget.id}/data`, { signal }),
  });
}

/* ---------------- saved queries ---------------- */

export function useSavedQueries(org: string, enabled = true) {
  return useQuery({
    queryKey: dashKeys.savedQueries(org),
    enabled,
    retry: 1,
    queryFn: async ({ signal }) => unwrapList(await api.get<SavedQuery[] | Paginated<SavedQuery>>(`${o(org)}/saved-queries`, { signal, query: { limit: 200 } })),
  });
}
