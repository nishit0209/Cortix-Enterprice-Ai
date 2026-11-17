import { NextResponse, type NextRequest } from "next/server";

/**
 * Edge guard: visitors without a Django session cookie are redirected to /login before any app route renders.
 *
 * The cookie's *validity* is verified client-side (GET /me). An expired session triggers the same redirect from the
 * API layer's 401 handler. Auth pages are intentionally not redirected here based on the cookie alone: a stale
 * (HttpOnly, unreadable) cookie would otherwise cause a /login <-> / redirect loop. The login page itself forwards
 * signed-in users after confirming the session with /me.
 */
const SESSION_COOKIE = process.env.SESSION_COOKIE_NAME || "sessionid";

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  if (req.cookies.get(SESSION_COOKIE)?.value) return NextResponse.next();

  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.search = `?next=${encodeURIComponent(pathname + search)}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/o", "/o/:path*", "/onboarding"],
};
