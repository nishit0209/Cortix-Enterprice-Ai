"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { postEventStream } from "@/lib/post-stream";
import { biKeys, normalizeMessage } from "@/lib/queries/bi";
import { chatReducer, initialChatState, nextStepAfterStream, toChatEvent, type ChatAction, type ChatStreamState } from "@/lib/chat/reducer";
import type { ChatMessage } from "@/lib/types";

export interface LiveTurn {
  turnId: string;
  conversationId: string;
  question: string;
  state: ChatStreamState;
  /** Message ids that existed when the question was sent; newer persisted messages belong to this turn. */
  baseline: Set<string>;
}

let turnSeq = 0;

/**
 * Sends a question over the chat SSE endpoint and folds the stream into `live`.
 * Never re-POSTs automatically (see nextStepAfterStream): an interrupted stream is reconciled by refetching
 * the conversation's messages, because the server keeps working and persists the answer.
 */
export function useChat(org: string) {
  const qc = useQueryClient();
  const [live, setLive] = React.useState<LiveTurn | null>(null);
  const ctrlRef = React.useRef<AbortController | null>(null);
  /** Final live states by message id: fills gaps if a persisted message lacks streamed-only details. */
  const finishedRef = React.useRef(new Map<string, ChatStreamState>());

  React.useEffect(() => () => ctrlRef.current?.abort(), []);

  const refetchMessages = React.useCallback(
    (conversationId: string) => qc.invalidateQueries({ queryKey: biKeys.messages(org, conversationId) }),
    [org, qc],
  );

  const ask = React.useCallback(
    async (conversationId: string, question: string) => {
      ctrlRef.current?.abort();
      const ctrl = new AbortController();
      ctrlRef.current = ctrl;
      const turnId = `turn-${++turnSeq}`;
      const existing = qc.getQueryData<ChatMessage[]>(biKeys.messages(org, conversationId)) ?? [];
      let state = initialChatState();
      setLive({ turnId, conversationId, question, state, baseline: new Set(existing.map((m) => m.id)) });
      const dispatch = (a: ChatAction) => {
        state = chatReducer(state, a);
        setLive((l) => (l && l.turnId === turnId ? { ...l, state } : l));
      };

      const outcome = await postEventStream({
        url: `/orgs/${encodeURIComponent(org)}/bi/conversations/${conversationId}/messages`,
        body: { question },
        signal: ctrl.signal,
        onOpen: () => dispatch({ kind: "open" }),
        onMessage: (msg) => {
          const ev = toChatEvent(msg.event, msg.data);
          if (ev) dispatch({ kind: "event", event: ev });
        },
      });

      switch (outcome.kind) {
        case "rejected":
          dispatch({ kind: "rejected", error: outcome.error });
          break;
        case "interrupted":
          dispatch({ kind: "interrupted", error: outcome.error });
          break;
        case "aborted":
          dispatch({ kind: "aborted" });
          break;
        case "json": {
          // Synchronous fallback (JSON Accept): the body is the persisted assistant Message.
          const body = outcome.body as Partial<ChatMessage> | null;
          if (body && typeof body === "object" && typeof body.id === "string") {
            const m = normalizeMessage({ ...body, id: body.id, role: "assistant" });
            dispatch({ kind: "open" });
            dispatch({ kind: "event", event: { type: "done", data: { message_id: m.id } } });
          }
          break;
        }
        default:
          break;
      }
      if (state.messageId) finishedRef.current.set(state.messageId, state);
      if (ctrlRef.current === ctrl) ctrlRef.current = null;

      void qc.invalidateQueries({ queryKey: biKeys.conversations(org) });
      await refetchMessages(conversationId);
      if (nextStepAfterStream(state) === "reconcile") {
        // The server accepted the question; poll briefly for the persisted answer.
        for (const delay of [1500, 3000, 6000]) {
          await new Promise((r) => setTimeout(r, delay));
          if (ctrlRef.current && ctrlRef.current !== ctrl) break; // a newer question took over
          await refetchMessages(conversationId);
          const msgs = qc.getQueryData<ChatMessage[]>(biKeys.messages(org, conversationId)) ?? [];
          if (msgs.some((m) => m.role === "assistant" && !existing.some((e) => e.id === m.id))) break;
        }
      }
    },
    [org, qc, refetchMessages],
  );

  const stop = React.useCallback(() => ctrlRef.current?.abort(), []);
  const clear = React.useCallback(() => setLive(null), []);

  return { live, ask, stop, clear, finished: finishedRef.current };
}

/**
 * Merge persisted messages with the live turn: while a turn is live, messages newer than its baseline are hidden
 * (the live bubbles represent them) until the persisted assistant answer for the turn exists.
 */
export function mergeLive(messages: ChatMessage[], live: LiveTurn | null, conversationId: string | null) {
  if (!live || live.conversationId !== conversationId) return { messages, showLive: false };
  const fresh = messages.filter((m) => !live.baseline.has(m.id));
  const persistedAnswer =
    fresh.find((m) => m.role === "assistant" && (live.state.messageId ? m.id === live.state.messageId : true)) ?? null;
  const streaming = live.state.phase === "connecting" || live.state.phase === "streaming";
  if (persistedAnswer && !streaming) return { messages, showLive: false };
  return { messages: messages.filter((m) => live.baseline.has(m.id)), showLive: true };
}
