"use client";

import * as React from "react";
import Link from "next/link";
import { Bell, BellOff } from "lucide-react";
import { useActivity } from "@/lib/queries/org";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/overlays";
import { Button } from "@/components/ui/button";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { ActivityList } from "@/components/activity/activity-list";
import { isUnavailableEndpoint } from "@/lib/errors";
import { useOrgContext } from "./org-context";

const seenKey = (org: string) => `nexora:activity-seen:${org}`;

function readSeen(org: string): number {
  try {
    return Number(window.localStorage.getItem(seenKey(org)) ?? 0) || 0;
  } catch {
    return 0;
  }
}

export function NotificationCenter() {
  const { slug, can } = useOrgContext();
  const q = useActivity(slug);
  const [open, setOpen] = React.useState(false);
  const [seenAt, setSeenAt] = React.useState(0);

  React.useEffect(() => setSeenAt(readSeen(slug)), [slug]);

  const events = React.useMemo(() => q.data ?? [], [q.data]);
  const unread = events.filter((e) => new Date(e.occurred_at).getTime() > seenAt).length;

  const markSeen = React.useCallback(() => {
    const latest = events.reduce((m, e) => Math.max(m, new Date(e.occurred_at).getTime()), Date.now());
    setSeenAt(latest);
    try {
      window.localStorage.setItem(seenKey(slug), String(latest));
    } catch {
      /* ignore */
    }
  }, [events, slug]);

  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) markSeen();
      }}
    >
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"} className="relative">
          <Bell />
          {unread > 0 && (
            <span className="tabular absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-accent-fg ring-2 ring-bg">
              {unread > 9 ? "9+" : unread}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(380px,calc(100vw-24px))] p-0">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <span className="text-sm font-semibold text-fg">Activity</span>
          {unread > 0 && (
            <button type="button" onClick={markSeen} className="text-xs font-medium text-accent hover:underline">
              Mark all as read
            </button>
          )}
        </div>
        <div className="scrollbar-thin max-h-[420px] overflow-y-auto px-4">
          {q.isLoading ? (
            <div className="space-y-4 py-4">
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
          ) : q.isError ? (
            <EmptyState
              compact
              icon={<BellOff />}
              title={isUnavailableEndpoint(q.error) ? "Activity feed unavailable" : "Couldn't load activity"}
              description="Notifications appear here when the activity service responds."
            />
          ) : events.length === 0 ? (
            <EmptyState compact icon={<Bell />} title="You're all caught up" description="New activity in this organization shows up here." />
          ) : (
            <ActivityList events={events} compact max={25} />
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
