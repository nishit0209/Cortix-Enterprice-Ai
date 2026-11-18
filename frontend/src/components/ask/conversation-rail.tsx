"use client";

import * as React from "react";
import { History, MessageSquare, MoreHorizontal, Pencil, Plus, Search, SquareTerminal, Trash2 } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/overlays";
import { groupConversations } from "@/lib/chat/groups";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { useDeleteConversation, useRenameConversation } from "@/lib/queries/bi";
import type { Conversation, Dataset } from "@/lib/types";
import { cn, formatRelative } from "@/lib/utils";
import { DatasetPicker } from "./dataset-picker";

export function ConversationRail({
  slug,
  conversations,
  loading,
  error,
  onRetry,
  selectedId,
  onSelect,
  onNew,
  datasets,
  draftDatasetId,
  onDraftDataset,
  canSql,
}: {
  slug: string;
  conversations: Conversation[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  datasets: Dataset[];
  draftDatasetId: string | null;
  onDraftDataset: (id: string) => void;
  canSql: boolean;
}) {
  const [query, setQuery] = React.useState("");
  const [renaming, setRenaming] = React.useState<string | null>(null);
  const [deleting, setDeleting] = React.useState<Conversation | null>(null);
  const rename = useRenameConversation(slug);
  const del = useDeleteConversation(slug);
  const names = React.useMemo(() => new Map(datasets.map((d) => [d.id, d.name])), [datasets]);
  const filtered = query
    ? conversations.filter((c) => `${c.title} ${names.get(c.dataset_id) ?? ""}`.toLowerCase().includes(query.toLowerCase()))
    : conversations;
  const groups = groupConversations(filtered);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 border-b border-border p-3">
        <Button className="w-full justify-start" onClick={onNew}>
          <Plus /> New conversation
        </Button>
        <DatasetPicker datasets={datasets} value={draftDatasetId} onChange={onDraftDataset} label="Dataset for new conversations" />
        <Input
          aria-label="Search conversations"
          placeholder="Search conversations…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          leading={<Search />}
        />
      </div>
      <nav className="scrollbar-thin min-h-0 flex-1 overflow-y-auto p-2" aria-label="Conversations">
        {loading ? (
          <div className="space-y-2 p-1">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : error ? (
          isUnavailableEndpoint(error) ? (
            <EmptyState compact icon={<MessageSquare />} title="Conversations aren't available yet" description="The BI service hasn't been deployed." />
          ) : (
            <ErrorState compact error={error} onRetry={onRetry} />
          )
        ) : groups.length === 0 ? (
          <EmptyState
            compact
            icon={<MessageSquare />}
            title={query ? "No matches" : "No conversations yet"}
            description={query ? "Try another search." : "Ask your first question to start one."}
          />
        ) : (
          groups.map((g) => (
            <div key={g.label} className="mb-3">
              <div className="px-2 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-subtle">{g.label}</div>
              <ul className="space-y-0.5">
                {g.items.map((c) => (
                  <li key={c.id} className="group/conv relative">
                    {renaming === c.id ? (
                      <form
                        onSubmit={async (e) => {
                          e.preventDefault();
                          const title = String(new FormData(e.currentTarget).get("title") ?? "").trim();
                          setRenaming(null);
                          if (!title || title === c.title) return;
                          try {
                            await rename.mutateAsync({ id: c.id, title });
                          } catch (err) {
                            toast.error(errorMessage(err));
                          }
                        }}
                      >
                        <Input
                          name="title"
                          aria-label="Conversation title"
                          defaultValue={c.title}
                          autoFocus
                          maxLength={120}
                          onBlur={(e) => e.currentTarget.form?.requestSubmit()}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") setRenaming(null);
                          }}
                          className="h-9"
                        />
                      </form>
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => onSelect(c.id)}
                          aria-current={selectedId === c.id ? "page" : undefined}
                          className={cn(
                            "flex w-full flex-col rounded-control px-2.5 py-2 pr-9 text-left transition-colors focus-ring",
                            selectedId === c.id ? "bg-accent-soft text-accent-soft-fg" : "text-fg hover:bg-surface-2",
                          )}
                        >
                          <span className="truncate text-sm font-medium">{c.title || "Untitled conversation"}</span>
                          <span className={cn("truncate text-xs", selectedId === c.id ? "text-accent-soft-fg/80" : "text-fg-subtle")}>
                            {names.get(c.dataset_id) ?? "Dataset"} · {formatRelative(c.last_message_at ?? c.created_at)}
                          </span>
                        </button>
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <button
                              type="button"
                              aria-label={`Actions for ${c.title}`}
                              className="absolute right-1.5 top-1/2 flex size-7 -translate-y-1/2 items-center justify-center rounded-control text-fg-subtle opacity-0 transition-opacity hover:bg-surface-3 hover:text-fg focus-ring focus:opacity-100 group-hover/conv:opacity-100 data-[state=open]:opacity-100"
                            >
                              <MoreHorizontal className="size-4" />
                            </button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem onSelect={() => setRenaming(c.id)}>
                              <Pencil /> Rename
                            </DropdownMenuItem>
                            <DropdownMenuItem destructive onSelect={() => setDeleting(c)}>
                              <Trash2 /> Delete
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </nav>
      <div className="flex gap-1 border-t border-border p-2">
        <Button variant="ghost" size="sm" className="flex-1" asChild>
          <Link href={`/o/${slug}/ask/history`}>
            <History /> History
          </Link>
        </Button>
        {canSql && (
          <Button variant="ghost" size="sm" className="flex-1" asChild>
            <Link href={`/o/${slug}/ask/sql`}>
              <SquareTerminal /> SQL editor
            </Link>
          </Button>
        )}
      </div>
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete “${deleting?.title ?? ""}”?`}
        description="The conversation and its answers are removed. The query audit log is kept."
        confirmLabel="Delete conversation"
        loading={del.isPending}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await del.mutateAsync(deleting.id);
            toast.success("Conversation deleted");
            if (selectedId === deleting.id) onNew();
            setDeleting(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}
