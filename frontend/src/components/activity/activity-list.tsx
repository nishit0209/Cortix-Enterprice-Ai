"use client";

import * as React from "react";
import {
  Bot,
  Database,
  FileUp,
  KeyRound,
  LogIn,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
  Workflow,
  type LucideIcon,
  Activity,
} from "lucide-react";
import { cn, formatDateTime, formatRelative, humanizeAction } from "@/lib/utils";
import type { ActivityEvent } from "@/lib/types";
import { Avatar } from "@/components/ui/avatar";
import { Tooltip } from "@/components/ui/overlays";

function iconFor(action: string): LucideIcon {
  const a = action.toLowerCase();
  if (a.includes("delete") || a.includes("remove") || a.includes("revoke")) return Trash2;
  if (a.includes("invite") || a.includes("member")) return a.includes("invite") ? UserPlus : Users;
  if (a.includes("ai_policy") || a.includes("consent") || a.includes("policy")) return ShieldCheck;
  if (a.includes("upload")) return FileUp;
  if (a.includes("dataset")) return Database;
  if (a.includes("pipeline")) return Workflow;
  if (a.includes("api_key")) return KeyRound;
  if (a.includes("login") || a.includes("auth")) return LogIn;
  if (a.includes("ai") || a.includes("gemini")) return Bot;
  return Activity;
}

export function actorLabel(e: ActivityEvent): string {
  if (e.actor_name) return e.actor_name;
  if (e.actor_email) return e.actor_email;
  if (e.actor_type === "system") return "System";
  if (e.actor_type === "api_key") return "API key";
  return "A member";
}

function targetLabel(e: ActivityEvent): string | null {
  const meta = e.metadata ?? {};
  const name = (meta.name ?? meta.dataset_name ?? meta.email ?? meta.filename) as string | undefined;
  if (name) return String(name);
  if (e.target_type) return e.target_type.replace(/_/g, " ");
  return null;
}

export function ActivityItem({ event, compact }: { event: ActivityEvent; compact?: boolean }) {
  const Icon = iconFor(event.action);
  const actor = actorLabel(event);
  const target = targetLabel(event);
  return (
    <li className={cn("flex gap-3", compact ? "py-2.5" : "py-3")}>
      <div className="relative shrink-0">
        <Avatar name={actor} size={compact ? 28 : 32} />
        <span className="absolute -bottom-1 -right-1 flex size-4 items-center justify-center rounded-full border border-surface bg-surface-2 text-fg-muted">
          <Icon className="size-2.5" aria-hidden />
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-sm leading-5 text-fg">
          <span className="font-medium">{actor}</span>{" "}
          <span className="text-fg-muted">{humanizeAction(event.action).toLowerCase()}</span>
          {target && (
            <>
              {" "}
              <span className="font-medium">{target}</span>
            </>
          )}
        </p>
        <Tooltip content={formatDateTime(event.occurred_at)} side="bottom" align="start">
          <time dateTime={event.occurred_at} className="text-xs text-fg-subtle">
            {formatRelative(event.occurred_at)}
          </time>
        </Tooltip>
      </div>
    </li>
  );
}

export function ActivityList({ events, compact, max }: { events: ActivityEvent[]; compact?: boolean; max?: number }) {
  const list = max ? events.slice(0, max) : events;
  return (
    <ul className="divide-y divide-border" aria-label="Recent activity">
      {list.map((e) => (
        <ActivityItem key={e.id} event={e} compact={compact} />
      ))}
    </ul>
  );
}
