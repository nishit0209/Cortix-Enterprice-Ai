"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrapList } from "@/lib/api";
import { postDownload } from "@/lib/download";
import type { ChatMessage, Conversation, ManualQueryResult, Paginated, QueryHistoryItem } from "@/lib/types";

const o = (org: string) => `/orgs/${encodeURIComponent(org)}`;

export const biKeys = {
  conversations: (org: string) => ["org", org, "bi", "conversations"] as const,
  messages: (org: string, id: string) => ["org", org, "bi", "conversations", id, "messages"] as const,
  history: (org: string, filters: Record<string, string>) => ["org", org, "bi", "query-history", filters] as const,
};

/** Normalize a message from the API (tolerates missing optional arrays). */
export function normalizeMessage(m: Partial<ChatMessage> & { id: string; role: ChatMessage["role"] }): ChatMessage {
  return {
    content: "",
    status: "complete",
    error_code: null,
    created_at: new Date().toISOString(),
    sql: null,
    result: null,
    chart: null,
    ...m,
    insights: Array.isArray(m.insights) ? m.insights : [],
    assumptions: Array.isArray(m.assumptions) ? m.assumptions : [],
  } as ChatMessage;
}

export function useConversations(org: string, enabled = true) {
  return useQuery({
    queryKey: biKeys.conversations(org),
    enabled,
    queryFn: async ({ signal }) =>
      unwrapList(await api.get<Conversation[] | Paginated<Conversation>>(`${o(org)}/bi/conversations`, { signal, query: { limit: 200 } })),
  });
}

export function useMessages(org: string, conversationId: string | null) {
  return useQuery({
    queryKey: biKeys.messages(org, conversationId ?? ""),
    enabled: !!conversationId,
    queryFn: async ({ signal }) =>
      unwrapList(
        await api.get<ChatMessage[] | Paginated<ChatMessage>>(`${o(org)}/bi/conversations/${conversationId}/messages`, { signal }),
      ).map(normalizeMessage),
  });
}

export function useCreateConversation(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { dataset_id: string; title?: string }) => api.post<Conversation>(`${o(org)}/bi/conversations`, body),
    onSuccess: (conv) => {
      qc.setQueryData<Conversation[]>(biKeys.conversations(org), (old) => [conv, ...(old ?? []).filter((c) => c.id !== conv.id)]);
    },
  });
}

export function useRenameConversation(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => api.patch<Conversation>(`${o(org)}/bi/conversations/${id}`, { title }),
    onMutate: async ({ id, title }) => {
      await qc.cancelQueries({ queryKey: biKeys.conversations(org) });
      const prev = qc.getQueryData<Conversation[]>(biKeys.conversations(org));
      qc.setQueryData<Conversation[]>(biKeys.conversations(org), (old) => (old ?? []).map((c) => (c.id === id ? { ...c, title } : c)));
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(biKeys.conversations(org), ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: biKeys.conversations(org) }),
  });
}

export function useDeleteConversation(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`${o(org)}/bi/conversations/${id}`),
    onSuccess: (_d, id) => {
      qc.setQueryData<Conversation[]>(biKeys.conversations(org), (old) => (old ?? []).filter((c) => c.id !== id));
      qc.removeQueries({ queryKey: biKeys.messages(org, id) });
    },
  });
}

export function useMessageFeedback(org: string) {
  return useMutation({
    // Backend FeedbackSerializer: {value: "up" | "down"}.
    mutationFn: ({ messageId, correct }: { messageId: string; correct: boolean }) =>
      api.post<unknown>(`${o(org)}/bi/messages/${messageId}/feedback`, { value: correct ? "up" : "down" }),
  });
}

export function useManualQuery(org: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { dataset_id: string; sql: string }) => api.post<ManualQueryResult>(`${o(org)}/bi/queries`, body),
    onSettled: () => qc.invalidateQueries({ queryKey: ["org", org, "bi", "query-history"] }),
  });
}

export function useQueryHistory(org: string, filters: { limit?: number } = {}, enabled = true) {
  const clean: Record<string, string> = {};
  if (filters.limit) clean.limit = String(filters.limit);
  return useQuery({
    queryKey: biKeys.history(org, clean),
    enabled,
    retry: 1,
    queryFn: async ({ signal }) =>
      unwrapList(
        await api.get<QueryHistoryItem[] | Paginated<QueryHistoryItem>>(`${o(org)}/bi/query-history`, {
          signal,
          query: { limit: filters.limit ?? 200 },
        }),
      ),
  });
}

export function exportMessage(org: string, messageId: string, format: "csv" | "xlsx") {
  return postDownload(`${o(org)}/exports`, { message_id: messageId, format }, `cortix-answer-${messageId.slice(0, 8)}.${format}`);
}

export function historyDatasetName(item: QueryHistoryItem): string {
  if (!item.dataset) return "—";
  return typeof item.dataset === "string" ? item.dataset : item.dataset.name;
}

export function historyDatasetId(item: QueryHistoryItem): string | null {
  if (!item.dataset || typeof item.dataset === "string") return null;
  return item.dataset.id;
}
