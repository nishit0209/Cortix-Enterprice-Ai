"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertTriangle, Bell, BellOff, CheckCheck, Database, FileText, GitBranch, Sparkles, UserPlus } from "lucide-react";
import { toast } from "sonner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/overlays";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { isUnavailableEndpoint, errorMessage } from "@/lib/errors";
import { badgeLabel, notificationHref } from "@/lib/notifications";
import { useMarkAllNotificationsRead, useMarkNotificationRead, useNotifications } from "@/lib/queries/notifications";
import type { AppNotification } from "@/lib/types";
import { cn, formatRelative } from "@/lib/utils";
import { useOrgContext } from "./org-context";

function KindIcon({ kind }: { kind: string }) {
  const k = kind.toLowerCase();
  const failed = /fail|error|exhaust/.test(k);
  const Icon = k.includes("pipeline")
    ? GitBranch
    : k.includes("document")
      ? FileText
      : k.includes("invitation") || k.includes("member")
        ? UserPlus
        : k.includes("quota") || k.includes("ai")
          ? Sparkles
          : k.includes("import") || k.includes("dataset") || k.includes("ingest")
            ? Database
            : Bell;
  return (
    <span
      className={cn(
        "flex size-7 shrink-0 items-center justify-center rounded-full",
        failed ? "bg-danger-soft text-danger" : "bg-accent-soft text-accent-soft-fg",
      )}
      aria-hidden
    >
      {failed ? <AlertTriangle className="size-3.5" /> : <Icon className="size-3.5" />}
    </span>
  );
}

/** Bell in the top bar: the caller's notifications from `/notifications` (poll 30 s + refetch on focus). */
export function NotificationCenter() {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const q = useNotifications(slug);
  const markRead = useMarkNotificationRead(slug);
  const markAll = useMarkAllNotificationsRead(slug);
  const [open, setOpen] = React.useState(false);
  const items = q.data?.items ?? [];
  const unread = q.data?.unreadCount ?? 0;
  const badge = badgeLabel(unread);

  const openItem = (n: AppNotification) => {
    if (!n.read_at) markRead.mutate(n.id, { onError: (e) => toast.error(errorMessage(e)) });
    const href = notificationHref(n.link, slug);
    if (href) {
      setOpen(false);
      router.push(href);
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"} className="relative">
          <Bell />
          {badge && (
            <span className="tabular absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-accent-fg ring-2 ring-bg">
              {badge}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(400px,calc(100vw-24px))] p-0">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <span className="text-sm font-semibold text-fg">
            Notifications{unread > 0 && <span className="ml-1.5 text-xs font-normal text-fg-muted">{unread} unread</span>}
          </span>
          {unread > 0 && (
            <Button
              variant="ghost"
              size="xs"
              loading={markAll.isPending}
              onClick={() => markAll.mutate(undefined, { onError: (e) => toast.error(errorMessage(e)) })}
            >
              <CheckCheck /> Mark all read
            </Button>
          )}
        </div>
        <div className="scrollbar-thin max-h-[440px] overflow-y-auto">
          {q.isLoading ? (
            <div className="space-y-4 p-4">
              {[0, 1, 2].map((i) => (
                <div key={i} className="flex gap-3">
                  <Skeleton className="size-7 rounded-full" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-3.5 w-4/5" />
                    <Skeleton className="h-3 w-1/3" />
                  </div>
                </div>
              ))}
            </div>
          ) : q.isError && !q.data ? (
            isUnavailableEndpoint(q.error) ? (
              <EmptyState compact icon={<BellOff />} title="Notifications aren't available" description="The notification service did not respond. Try again shortly." />
            ) : (
              <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
            )
          ) : items.length === 0 ? (
            <EmptyState
              compact
              icon={<Bell />}
              title="You're all caught up"
              description="Pipeline runs, imports, processed documents, accepted invitations and AI quota alerts show up here."
            />
          ) : (
            <ul className="divide-y divide-border" aria-label="Notifications">
              {items.map((n) => {
                const href = notificationHref(n.link, slug);
                return (
                  <li key={n.id}>
                    <button
                      type="button"
                      onClick={() => openItem(n)}
                      className={cn(
                        "flex w-full gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-2 focus-ring",
                        !n.read_at && "bg-accent-soft/30",
                        !href && n.read_at && "cursor-default",
                      )}
                      aria-label={`${n.read_at ? "" : "Unread: "}${n.title}`}
                    >
                      <KindIcon kind={n.kind} />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-start gap-2">
                          <span className={cn("min-w-0 flex-1 text-sm text-fg", !n.read_at && "font-semibold")}>{n.title}</span>
                          {!n.read_at && <span className="mt-1.5 size-2 shrink-0 rounded-full bg-accent" aria-hidden />}
                        </span>
                        {n.body && <span className="mt-0.5 line-clamp-2 block text-xs text-fg-muted">{n.body}</span>}
                        <span className="mt-1 block text-[11px] text-fg-subtle" title={n.created_at}>
                          {formatRelative(n.created_at)}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        {can("audit.view") && (
          <div className="border-t border-border px-4 py-2.5">
            <Link href={`/o/${slug}/audit`} onClick={() => setOpen(false)} className="text-xs font-medium text-accent hover:underline">
              Open audit log →
            </Link>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
