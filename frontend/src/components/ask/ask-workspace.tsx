"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { motion, useReducedMotion } from "framer-motion";
import { ArrowDown, Database, MessagesSquare, PanelLeft, Sparkles, Upload, X } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { ClassificationBadge, FeatureStatusBadge } from "@/components/ui/status";
import { useChat, mergeLive } from "@/hooks/use-chat";
import { exampleQuestions } from "@/lib/chat/suggestions";
import { titleFromQuestion } from "@/lib/chat/groups";
import { viewFromLive, viewFromMessage } from "@/lib/chat/view";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { useConversations, useCreateConversation, useMessages } from "@/lib/queries/bi";
import { useDatasetColumns, useDatasets } from "@/lib/queries/datasets";
import { useAiStatus } from "@/lib/queries/org";
import type { ChatMessage, Dataset } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AnswerCard, UserBubble } from "./answer-card";
import { Composer, type ComposerHandle } from "./composer";
import { ConversationRail } from "./conversation-rail";
import { AiContextChip } from "./ai-context";
import { isReadyDataset } from "./dataset-picker";

const DRAFT_KEY = "nexora:ask:dataset";

function readDraft(): string | null {
  try {
    return window.localStorage.getItem(DRAFT_KEY);
  } catch {
    return null;
  }
}

function writeDraft(id: string) {
  try {
    window.localStorage.setItem(DRAFT_KEY, id);
  } catch {
    /* per-viewer convenience only */
  }
}

export function AskWorkspace() {
  const { slug, can, org } = useOrgContext();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const selectedId = params.get("c");
  const reduce = useReducedMotion();

  const conversations = useConversations(slug);
  const datasets = useDatasets(slug);
  const ai = useAiStatus(slug);
  const messages = useMessages(slug, selectedId);
  const createConv = useCreateConversation(slug);
  const chat = useChat(slug);
  const composerRef = React.useRef<ComposerHandle>(null);
  const [railOpen, setRailOpen] = React.useState(false);
  const [draftDatasetId, setDraftDatasetId] = React.useState<string | null>(params.get("dataset"));

  const allDatasets = React.useMemo(() => datasets.data ?? [], [datasets.data]);
  const readyDatasets = React.useMemo(() => allDatasets.filter(isReadyDataset), [allDatasets]);
  const conversation = (conversations.data ?? []).find((c) => c.id === selectedId) ?? null;
  const activeDatasetId = conversation?.dataset_id ?? draftDatasetId;
  const dataset: Dataset | undefined = allDatasets.find((d) => d.id === activeDatasetId);
  const columns = useDatasetColumns(slug, activeDatasetId ?? "", !!activeDatasetId && !!dataset && isReadyDataset(dataset));
  useSetBreadcrumb(conversation?.title ?? null);

  // Default dataset for new conversations: ?dataset=, then the last used one, then the first ready dataset.
  React.useEffect(() => {
    if (draftDatasetId && readyDatasets.some((d) => d.id === draftDatasetId)) return;
    if (!datasets.data) return;
    const remembered = readDraft();
    const pick = readyDatasets.find((d) => d.id === remembered) ?? readyDatasets[0];
    if (pick) setDraftDatasetId(pick.id);
  }, [datasets.data, readyDatasets, draftDatasetId]);

  const select = React.useCallback(
    (id: string | null) => {
      const sp = new URLSearchParams(params.toString());
      if (id) sp.set("c", id);
      else sp.delete("c");
      sp.delete("dataset");
      router.replace(`${pathname}${sp.toString() ? `?${sp}` : ""}`, { scroll: false });
      setRailOpen(false);
    },
    [params, pathname, router],
  );

  const streaming = !!chat.live && (chat.live.state.phase === "connecting" || chat.live.state.phase === "streaming");

  const send = React.useCallback(
    async (question: string) => {
      if (streaming) return;
      let convId = selectedId;
      if (!convId) {
        if (!draftDatasetId) {
          toast.error("Choose a dataset first.");
          return;
        }
        try {
          const conv = await createConv.mutateAsync({ dataset_id: draftDatasetId, title: titleFromQuestion(question) });
          convId = conv.id;
          writeDraft(draftDatasetId);
          select(conv.id);
        } catch (e) {
          toast.error(errorMessage(e));
          return;
        }
      }
      void chat.ask(convId, question);
    },
    [streaming, selectedId, draftDatasetId, createConv, select, chat],
  );

  // Switching conversation drops a finished live turn that belongs elsewhere.
  const { live, clear } = chat;
  React.useEffect(() => {
    if (live && live.conversationId !== selectedId && live.state.phase !== "streaming" && live.state.phase !== "connecting") clear();
  }, [selectedId, live, clear]);

  const merged = mergeLive(messages.data ?? [], chat.live, selectedId);
  const turns = buildTurns(merged.messages);
  const lastQuestion = chat.live?.question ?? [...turns].reverse().find((t) => t.question)?.question ?? null;

  // Auto-scroll: stick to the bottom while new content streams in, unless the user scrolled up.
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = React.useState(true);
  const contentKey = `${merged.messages.length}:${chat.live?.state.received ?? 0}:${chat.live?.state.phase ?? ""}`;
  React.useEffect(() => {
    const el = scrollRef.current;
    if (el && pinned) el.scrollTo({ top: el.scrollHeight, behavior: reduce ? "auto" : "smooth" });
  }, [contentKey, pinned, reduce]);

  const examples = React.useMemo(() => exampleQuestions(columns.data ?? []), [columns.data]);
  const errorCtx = {
    slug,
    policy: ai.data?.policy ?? org?.ai_data_policy ?? null,
    datasetId: activeDatasetId,
    classification: dataset?.classification ?? null,
    aiResetsAt: ai.data?.resets_at ?? null,
    canManagePolicy: can("org.ai_policy.consent"),
  };
  const noDatasets = datasets.isSuccess && readyDatasets.length === 0;
  const composerDisabled = !activeDatasetId || (!!conversation && !!dataset && !isReadyDataset(dataset));

  const rail = (
    <ConversationRail
      slug={slug}
      conversations={conversations.data ?? []}
      loading={conversations.isLoading}
      error={conversations.isError ? conversations.error : null}
      onRetry={() => void conversations.refetch()}
      selectedId={selectedId}
      onSelect={(id) => select(id)}
      onNew={() => {
        select(null);
        requestAnimationFrame(() => composerRef.current?.focus());
      }}
      datasets={allDatasets}
      draftDatasetId={draftDatasetId}
      onDraftDataset={(id) => {
        setDraftDatasetId(id);
        writeDraft(id);
        if (selectedId) select(null);
      }}
      canSql={can("bi.sql")}
    />
  );

  return (
    <div className="-mx-4 -my-6 flex h-[calc(100dvh-3.5rem)] min-h-[560px] sm:-mx-6 lg:-mx-8 lg:-my-8">
      {/* Rail (desktop) */}
      <aside className="hidden w-72 shrink-0 border-r border-border bg-surface-2/40 lg:block" aria-label="Conversations panel">
        {rail}
      </aside>
      {/* Rail (mobile drawer) */}
      <DialogPrimitive.Root open={railOpen} onOpenChange={setRailOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-overlay data-[state=open]:animate-fade lg:hidden" />
          <DialogPrimitive.Content
            className="fixed inset-y-0 left-0 z-50 flex w-[300px] max-w-[88vw] flex-col bg-surface shadow-pop focus:outline-none lg:hidden"
            aria-describedby={undefined}
          >
            <DialogPrimitive.Title className="sr-only">Conversations</DialogPrimitive.Title>
            <DialogPrimitive.Close
              className="absolute right-2 top-2 z-10 flex size-8 items-center justify-center rounded-control text-fg-muted hover:bg-surface-2 focus-ring"
              aria-label="Close conversations"
            >
              <X className="size-4" />
            </DialogPrimitive.Close>
            <div className="min-h-0 flex-1 pt-8">{rail}</div>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>

      <section className="flex min-w-0 flex-1 flex-col" aria-label="Chat">
        {/* Header */}
        <header className="flex items-center gap-2 border-b border-border px-3 py-2.5 sm:px-5">
          <Button variant="ghost" size="icon-sm" className="lg:hidden" onClick={() => setRailOpen(true)} aria-label="Open conversations">
            <PanelLeft />
          </Button>
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h1 className="truncate text-sm font-semibold text-fg sm:text-md">{conversation?.title ?? "New conversation"}</h1>
              <FeatureStatusBadge status="prototype" className="hidden sm:inline-flex" />
            </div>
            {dataset && (
              <div className="flex min-w-0 items-center gap-1.5 text-xs text-fg-muted">
                <Database className="size-3 shrink-0" aria-hidden />
                <Link href={`/o/${slug}/data/datasets/${dataset.id}`} className="truncate hover:text-fg hover:underline">
                  {dataset.name}
                </Link>
                <ClassificationBadge value={dataset.classification} />
              </div>
            )}
          </div>
          <AiContextChip slug={slug} status={ai.data} dataset={dataset} />
        </header>

        {/* Thread */}
        <div
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
          }}
          className="scrollbar-thin relative min-h-0 flex-1 overflow-y-auto"
        >
          <div className="mx-auto w-full max-w-3xl space-y-6 px-4 py-6 sm:px-6">
            {noDatasets ? (
              <Card>
                <EmptyState
                  icon={<Database />}
                  title="No ready datasets yet"
                  description="Upload a dataset and wait for profiling to finish. Then ask questions about it here."
                  action={
                    can("datasets.create") ? (
                      <Button asChild>
                        <Link href={`/o/${slug}/data/datasets?upload=1`}>
                          <Upload /> Upload data
                        </Link>
                      </Button>
                    ) : undefined
                  }
                />
              </Card>
            ) : selectedId && messages.isLoading ? (
              <ThreadSkeleton />
            ) : selectedId && messages.isError ? (
              isUnavailableEndpoint(messages.error) && !conversation ? (
                <EmptyState icon={<MessagesSquare />} title="Conversation not found" description="It may have been deleted." action={<Button variant="secondary" onClick={() => select(null)}>Start a new one</Button>} />
              ) : (
                <ErrorState error={messages.error} onRetry={() => void messages.refetch()} />
              )
            ) : turns.length === 0 && !merged.showLive ? (
              <Hero
                datasetName={dataset?.name}
                examples={examples}
                loading={columns.isLoading && !!activeDatasetId}
                onPick={(q) => void send(q)}
                disabled={composerDisabled}
              />
            ) : null}

            {turns.map((t) => (
              <div key={t.key} className="space-y-4">
                {t.question && <UserBubble text={t.question} />}
                {t.answer && (
                  <AnswerCard
                    view={viewFromMessage(t.answer, chat.finished.get(t.answer.id))}
                    question={t.question ?? ""}
                    ctx={errorCtx}
                    datasetColumns={columns.data ?? []}
                    onFollowUp={(q) => void send(q)}
                    canExport={can("exports.create")}
                  />
                )}
              </div>
            ))}

            {merged.showLive && chat.live && (
              <div className="space-y-4">
                <UserBubble text={chat.live.question} />
                <AnswerCard
                  key={chat.live.turnId}
                  view={viewFromLive(chat.live.turnId, chat.live.state)}
                  question={chat.live.question}
                  ctx={errorCtx}
                  datasetColumns={columns.data ?? []}
                  onFollowUp={(q) => void send(q)}
                  onRetry={
                    chat.live.state.phase === "error" || chat.live.state.phase === "interrupted" || chat.live.state.phase === "aborted"
                      ? () => void send(chat.live!.question)
                      : undefined
                  }
                  canExport={can("exports.create")}
                />
              </div>
            )}
          </div>
          {!pinned && (
            <button
              type="button"
              onClick={() => {
                setPinned(true);
                scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
              }}
              className="sticky bottom-3 left-1/2 z-10 mx-auto flex size-8 -translate-x-1/2 items-center justify-center rounded-full border border-border bg-surface text-fg-muted shadow-pop hover:text-fg focus-ring"
              aria-label="Scroll to latest"
            >
              <ArrowDown className="size-4" />
            </button>
          )}
        </div>

        {/* Composer */}
        <div className="border-t border-border bg-bg/80 px-4 pb-3 pt-3 backdrop-blur-sm sm:px-6">
          <div className="mx-auto w-full max-w-3xl">
            {!can("bi.chat") ? (
              <p className="py-3 text-center text-sm text-fg-muted">Your role doesn&apos;t include asking questions (bi.chat).</p>
            ) : (
              <Composer
                ref={composerRef}
                onSend={(q) => void send(q)}
                onStop={chat.stop}
                streaming={streaming || createConv.isPending}
                disabled={composerDisabled}
                disabledReason={noDatasets ? "Upload a dataset to start asking" : "Pick a ready dataset to start"}
                lastQuestion={lastQuestion}
                placeholder={selectedId ? "Ask a follow-up…" : dataset ? `Ask about ${dataset.name}…` : undefined}
              />
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

interface Turn {
  key: string;
  question: string | null;
  answer: ChatMessage | null;
}

/** Pair user questions with the assistant answers that follow them. */
export function buildTurns(messages: ChatMessage[]): Turn[] {
  const sorted = [...messages].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const turns: Turn[] = [];
  for (const m of sorted) {
    if (m.role === "user") turns.push({ key: m.id, question: m.content, answer: null });
    else {
      const last = turns[turns.length - 1];
      if (last && !last.answer) last.answer = m;
      else turns.push({ key: m.id, question: null, answer: m });
    }
  }
  return turns;
}

function ThreadSkeleton() {
  return (
    <div className="space-y-6" aria-busy>
      <div className="flex justify-end">
        <Skeleton className="h-10 w-2/3 rounded-[14px]" />
      </div>
      <div className="flex gap-3">
        <Skeleton className="size-8 rounded-full" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-48 w-full rounded-card" />
        </div>
      </div>
    </div>
  );
}

function Hero({
  datasetName,
  examples,
  loading,
  onPick,
  disabled,
}: {
  datasetName?: string;
  examples: string[];
  loading: boolean;
  onPick: (q: string) => void;
  disabled: boolean;
}) {
  const reduce = useReducedMotion();
  return (
    <div className="flex flex-col items-center pt-6 text-center sm:pt-14">
      <motion.div
        initial={reduce ? false : { opacity: 0, scale: 0.92 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.25 }}
        className="relative mb-5"
      >
        <span className="absolute inset-0 -z-10 scale-[2.2] rounded-full bg-accent/15 blur-2xl" aria-hidden />
        <span className="flex size-14 items-center justify-center rounded-[18px] brand-gradient text-white shadow-pop">
          <Sparkles className="size-6" aria-hidden />
        </span>
      </motion.div>
      <h2 className="text-xl font-semibold tracking-[-0.015em] text-fg">Ask {datasetName ? <span className="text-accent">{datasetName}</span> : "your data"} anything</h2>
      <p className="mt-2 max-w-md text-sm text-fg-muted">
        Answers come with the exact SQL that ran, a chart and locally computed insights. Your access policy is applied to every query.
      </p>
      <div className="mt-7 grid w-full max-w-xl gap-2 sm:grid-cols-2">
        {loading
          ? [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-14 rounded-card" />)
          : examples.map((q, i) => (
              <motion.button
                key={q}
                type="button"
                disabled={disabled}
                initial={reduce ? false : { opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.18, delay: 0.05 * i }}
                onClick={() => onPick(q)}
                className={cn(
                  "group rounded-card border border-border bg-surface px-4 py-3 text-left text-sm text-fg shadow-xs transition-[border-color,box-shadow,transform]",
                  "hover:-translate-y-0.5 hover:border-accent/40 hover:shadow-soft focus-ring disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                <span className="line-clamp-2">{q}</span>
              </motion.button>
            ))}
      </div>
    </div>
  );
}
