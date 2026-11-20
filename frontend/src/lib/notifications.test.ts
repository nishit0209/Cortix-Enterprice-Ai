import { describe, expect, it } from "vitest";
import { badgeLabel, emptyNotifications, notificationHref, notificationsReducer, NOTIFICATION_POLL_MS, type NotificationsState } from "./notifications";
import type { AppNotification } from "./types";

const n = (id: string, created_at: string, read_at: string | null = null): AppNotification => ({
  id,
  kind: "pipeline.run_failed",
  title: `Notification ${id}`,
  body: "",
  link: null,
  read_at,
  created_at,
});

const loaded = (items: AppNotification[], unreadCount?: number | null): NotificationsState =>
  notificationsReducer(emptyNotifications, { type: "loaded", items, unreadCount });

describe("notificationsReducer", () => {
  it("loads newest first, drops duplicates and trusts the server's unread_count", () => {
    const s = loaded([n("a", "2026-11-20T08:00:00Z"), n("b", "2026-11-20T09:00:00Z"), n("a", "2026-11-20T08:00:00Z")], 7);
    expect(s.items.map((i) => i.id)).toEqual(["b", "a"]);
    // The envelope counts every unread notification, not just this page.
    expect(s.unreadCount).toBe(7);
  });

  it("derives the unread count from the page when the server sends none", () => {
    const s = loaded([n("a", "2026-11-20T08:00:00Z"), n("b", "2026-11-20T09:00:00Z", "2026-11-20T09:05:00Z")], null);
    expect(s.unreadCount).toBe(1);
  });

  it("marks one read optimistically and is idempotent", () => {
    const s = loaded([n("a", "2026-11-20T08:00:00Z"), n("b", "2026-11-20T09:00:00Z")], 2);
    const once = notificationsReducer(s, { type: "markRead", id: "a", at: "2026-11-20T10:00:00Z" });
    expect(once.unreadCount).toBe(1);
    expect(once.items.find((i) => i.id === "a")?.read_at).toBe("2026-11-20T10:00:00Z");
    expect(notificationsReducer(once, { type: "markRead", id: "a", at: "2026-11-20T10:01:00Z" })).toBe(once);
    expect(notificationsReducer(once, { type: "markRead", id: "missing", at: "x" })).toBe(once);
  });

  it("never lets the badge go negative", () => {
    const s = loaded([n("a", "2026-11-20T08:00:00Z")], 0);
    expect(notificationsReducer(s, { type: "markRead", id: "a", at: "t" }).unreadCount).toBe(0);
    expect(notificationsReducer(s, { type: "serverCount", unreadCount: -3 }).unreadCount).toBe(0);
  });

  it("marks all read, keeping existing read times", () => {
    const s = loaded([n("a", "2026-11-20T08:00:00Z", "2026-11-20T08:30:00Z"), n("b", "2026-11-20T09:00:00Z")], 4);
    const all = notificationsReducer(s, { type: "markAllRead", at: "2026-11-20T10:00:00Z" });
    expect(all.unreadCount).toBe(0);
    expect(all.items.map((i) => i.read_at)).toEqual(["2026-11-20T10:00:00Z", "2026-11-20T08:30:00Z"]);
    expect(notificationsReducer(all, { type: "markAllRead", at: "later" })).toBe(all);
  });

  it("reconciles with the unread_count returned by POST /read", () => {
    const s = loaded([n("a", "2026-11-20T08:00:00Z")], 5);
    const next = notificationsReducer(s, { type: "serverCount", unreadCount: 3 });
    expect(next.unreadCount).toBe(3);
    expect(notificationsReducer(next, { type: "serverCount", unreadCount: 3 })).toBe(next);
  });

  it("polls every 30 seconds", () => {
    expect(NOTIFICATION_POLL_MS).toBe(30_000);
  });
});

describe("badgeLabel", () => {
  it("hides zero and caps at 99+", () => {
    expect(badgeLabel(0)).toBeNull();
    expect(badgeLabel(-1)).toBeNull();
    expect(badgeLabel(5)).toBe("5");
    expect(badgeLabel(99)).toBe("99");
    expect(badgeLabel(100)).toBe("99+");
  });
});

describe("notificationHref", () => {
  it("resolves producer paths under the current org and maps resource paths to their pages", () => {
    expect(notificationHref("/settings/members", "acme")).toBe("/o/acme/settings/members");
    expect(notificationHref("/documents/d1", "acme")).toBe("/o/acme/documents/d1");
    expect(notificationHref("/settings/ai", "acme")).toBe("/o/acme/settings/ai-policy");
    expect(notificationHref("/pipelines/p1/runs/r1", "acme")).toBe("/o/acme/data/pipelines/p1/runs/r1");
    expect(notificationHref("/datasets/d9?tab=quality", "acme")).toBe("/o/acme/data/datasets/d9?tab=quality");
    expect(notificationHref("/data-sources/s1", "acme")).toBe("/o/acme/data/sources");
    expect(notificationHref("/pipeline-runs/r1", "acme")).toBe("/o/acme/data/pipelines");
    // Whole segments only: `/settings/ai-policy` is already a page.
    expect(notificationHref("/settings/ai-policy", "acme")).toBe("/o/acme/settings/ai-policy");
  });

  it("keeps org-scoped links in their own org", () => {
    expect(notificationHref("/o/beta/data/datasets/d1", "acme")).toBe("/o/beta/data/datasets/d1");
    expect(notificationHref("/o/beta/pipelines/p1", "acme")).toBe("/o/beta/data/pipelines/p1");
  });

  it("refuses anything that could leave the app", () => {
    expect(notificationHref("https://evil.example/x", "acme")).toBeNull();
    expect(notificationHref("//evil.example/x", "acme")).toBeNull();
    expect(notificationHref("/\\evil.example", "acme")).toBeNull();
    expect(notificationHref("javascript:alert(1)", "acme")).toBeNull();
    expect(notificationHref("", "acme")).toBeNull();
    expect(notificationHref(null, "acme")).toBeNull();
  });
});
