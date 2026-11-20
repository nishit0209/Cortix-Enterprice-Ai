"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { NOTIFICATION_POLL_MS, notificationsReducer, type NotificationsState } from "@/lib/notifications";
import type { AppNotification, Paginated } from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export const notificationKeys = {
  list: (org: string) => ["org", org, "notifications"] as const,
};

type ListResponse = Paginated<AppNotification> & { unread_count?: number };

/** Latest notifications + unread count. Polls every 30 s while the tab is visible and refetches on focus. */
export function useNotifications(org: string | undefined) {
  return useQuery({
    queryKey: notificationKeys.list(org ?? ""),
    enabled: !!org,
    retry: 1,
    refetchInterval: NOTIFICATION_POLL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    queryFn: async ({ signal }): Promise<NotificationsState> => {
      const res = await api.get<ListResponse | AppNotification[]>(`${o(org!)}/notifications`, { signal, query: { limit: 30 } });
      const items = Array.isArray(res) ? res : (res.items ?? []);
      const unreadCount = Array.isArray(res) ? null : (res.unread_count ?? null);
      return notificationsReducer({ items: [], unreadCount: 0 }, { type: "loaded", items, unreadCount });
    },
  });
}

function useOptimistic(org: string) {
  const qc = useQueryClient();
  const key = notificationKeys.list(org);
  return {
    qc,
    key,
    apply: async (action: Parameters<typeof notificationsReducer>[1]) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<NotificationsState>(key);
      if (prev) qc.setQueryData<NotificationsState>(key, notificationsReducer(prev, action));
      return { prev };
    },
  };
}

export function useMarkNotificationRead(org: string) {
  const { qc, key, apply } = useOptimistic(org);
  return useMutation({
    mutationFn: (id: string) => api.post<{ unread_count?: number }>(`${o(org)}/notifications/${id}/read`),
    onMutate: (id) => apply({ type: "markRead", id, at: new Date().toISOString() }),
    onError: (_e, _v, ctx) => ctx?.prev && qc.setQueryData(key, ctx.prev),
    onSuccess: (res) => {
      if (typeof res?.unread_count === "number") {
        qc.setQueryData<NotificationsState>(key, (s) => (s ? notificationsReducer(s, { type: "serverCount", unreadCount: res.unread_count! }) : s));
      }
    },
  });
}

export function useMarkAllNotificationsRead(org: string) {
  const { qc, key, apply } = useOptimistic(org);
  return useMutation({
    mutationFn: () => api.post<{ updated?: number; unread_count?: number }>(`${o(org)}/notifications/read-all`),
    onMutate: () => apply({ type: "markAllRead", at: new Date().toISOString() }),
    onError: (_e, _v, ctx) => ctx?.prev && qc.setQueryData(key, ctx.prev),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });
}
