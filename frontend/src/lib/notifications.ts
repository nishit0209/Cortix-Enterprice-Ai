/**
 * Notification bell state (pure). The list endpoint is `GET /orgs/{org}/notifications` (cursor-paginated,
 * newest first) and its envelope carries `unread_count`; read/read-all answer with the new `unread_count`.
 * Optimistic updates go through `notificationsReducer`, so the badge reacts instantly and the next poll
 * (every 30 s, and on window focus) reconciles with the server.
 */

import type { AppNotification } from "@/lib/types";

export const NOTIFICATION_POLL_MS = 30_000;

export interface NotificationsState {
  items: AppNotification[];
  unreadCount: number;
}

export const emptyNotifications: NotificationsState = { items: [], unreadCount: 0 };

export type NotificationsAction =
  | { type: "loaded"; items: AppNotification[]; unreadCount?: number | null }
  | { type: "markRead"; id: string; at: string }
  | { type: "markAllRead"; at: string }
  | { type: "serverCount"; unreadCount: number };

export function notificationsReducer(state: NotificationsState, action: NotificationsAction): NotificationsState {
  switch (action.type) {
    case "loaded": {
      const seen = new Set<string>();
      const items = action.items
        .filter((n) => n && n.id && !seen.has(n.id) && seen.add(n.id))
        .sort((a, b) => b.created_at.localeCompare(a.created_at));
      const unreadCount = typeof action.unreadCount === "number" ? action.unreadCount : items.filter((n) => !n.read_at).length;
      return { items, unreadCount };
    }
    case "markRead": {
      const target = state.items.find((n) => n.id === action.id);
      if (!target || target.read_at) return state;
      return {
        items: state.items.map((n) => (n.id === action.id ? { ...n, read_at: action.at } : n)),
        unreadCount: Math.max(0, state.unreadCount - 1),
      };
    }
    case "markAllRead":
      if (state.unreadCount === 0 && state.items.every((n) => n.read_at)) return state;
      return { items: state.items.map((n) => (n.read_at ? n : { ...n, read_at: action.at })), unreadCount: 0 };
    case "serverCount":
      return state.unreadCount === action.unreadCount ? state : { ...state, unreadCount: Math.max(0, action.unreadCount) };
    default:
      return state;
  }
}

/** Badge text: nothing at 0, the number up to 99, then "99+". */
export function badgeLabel(count: number): string | null {
  if (!count || count < 0) return null;
  return count > 99 ? "99+" : String(count);
}

/**
 * Producer paths that name a backend resource rather than a UI route, mapped to the page that shows it
 * (`apps.ai` links the quota notice to `/settings/ai`; `apps.core.notifications` documents
 * `/pipelines/<id>/runs/<run_id>`). Matched on whole path segments.
 */
const LINK_ALIASES: Array<{ from: string; to: string; keepSubpath: boolean }> = [
  { from: "/settings/ai", to: "/settings/ai-policy", keepSubpath: true },
  { from: "/pipelines", to: "/data/pipelines", keepSubpath: true },
  { from: "/datasets", to: "/data/datasets", keepSubpath: true },
  // No per-item page: a single run (without its pipeline id) or source opens the list.
  { from: "/pipeline-runs", to: "/data/pipelines", keepSubpath: false },
  { from: "/data-sources", to: "/data/sources", keepSubpath: false },
  { from: "/sources", to: "/data/sources", keepSubpath: false },
];

function routeFor(path: string): string {
  const cut = path.search(/[?#]/);
  const [p, rest] = cut < 0 ? [path, ""] : [path.slice(0, cut), path.slice(cut)];
  for (const { from, to, keepSubpath } of LINK_ALIASES) {
    if (p === from || p.startsWith(`${from}/`)) return `${to}${keepSubpath ? p.slice(from.length) : ""}${rest}`;
  }
  return path;
}

/**
 * Notification links are app-relative paths (the backend drops anything else). Org-scoped paths such as
 * `/data/pipelines/<id>/runs` are resolved under the current org; `/o/<org>/...` links keep their org.
 */
export function notificationHref(link: string | null | undefined, slug: string): string | null {
  const l = (link ?? "").trim();
  if (!l.startsWith("/") || l.startsWith("//") || l.startsWith("/\\")) return null;
  const scoped = /^\/o\/([^/?#]+)(.*)$/.exec(l);
  if (scoped) return `/o/${scoped[1]}${routeFor(scoped[2] || "/dashboard")}`;
  return `/o/${slug}${routeFor(l)}`;
}
