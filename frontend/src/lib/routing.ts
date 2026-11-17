import type { MeResponse } from "./types";

const LAST_ORG_KEY = "nexora:last-org";

/** Only same-origin relative paths are allowed as post-login redirects (prevents open redirects). */
export function safeNext(next: string | null | undefined): string | null {
  if (!next) return null;
  if (!next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return null;
  if (next.startsWith("/login") || next.startsWith("/signup")) return null;
  return next;
}

export function rememberOrg(slug: string) {
  try {
    window.localStorage.setItem(LAST_ORG_KEY, slug);
  } catch {
    /* ignore */
  }
}

/** Where a signed-in user lands: last used org, else first org, else onboarding. */
export function homeFor(me: MeResponse): string {
  if (!me.orgs.length) return "/onboarding";
  let last: string | null = null;
  try {
    last = window.localStorage.getItem(LAST_ORG_KEY);
  } catch {
    last = null;
  }
  const org = me.orgs.find((o) => o.slug === last) ?? me.orgs.find((o) => o.status === "active") ?? me.orgs[0];
  return `/o/${org.slug}/dashboard`;
}
