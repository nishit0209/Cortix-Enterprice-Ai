import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  __resetCsrfForTests,
  api,
  buildUrl,
  cursorFrom,
  ensureCsrf,
  onUnauthorized,
  readCookie,
  toApiError,
  unwrapList,
} from "./api";
import { describeError, errorMessage, isUnavailableEndpoint } from "./errors";

function json(body: unknown, init: ResponseInit & { contentType?: string } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": init.contentType ?? "application/json" },
  });
}

function setCookie(v: string) {
  document.cookie = v;
}

describe("buildUrl", () => {
  it("prefixes the API base and serialises query params, skipping empty values", () => {
    expect(buildUrl("/me")).toBe("/api/v1/me");
    expect(buildUrl("orgs/acme/datasets", { search: "sales", limit: 50, cursor: undefined, x: "" })).toBe(
      "/api/v1/orgs/acme/datasets?search=sales&limit=50",
    );
    expect(buildUrl("/api/v1/auth/csrf")).toBe("/api/v1/auth/csrf");
  });
});

describe("CSRF handling", () => {
  beforeEach(() => {
    __resetCsrfForTests();
  });

  it("reads cookies by name", () => {
    setCookie("csrftoken=abc123");
    setCookie("other=1");
    expect(readCookie("csrftoken")).toBe("abc123");
    expect(readCookie("missing")).toBeNull();
  });

  it("fetches /auth/csrf once when the cookie is missing, then sends X-CSRFToken on unsafe methods", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        if (url === "/api/v1/auth/csrf") {
          setCookie("csrftoken=tok-1");
          return new Response(null, { status: 204 });
        }
        return json({ id: "org-1", name: "Acme" }, { status: 201 });
      }),
    );

    await api.post("/orgs", { name: "Acme" });
    await api.post("/orgs", { name: "Beta" });

    const csrfCalls = calls.filter((c) => c.url === "/api/v1/auth/csrf");
    expect(csrfCalls).toHaveLength(1);
    const posts = calls.filter((c) => c.url === "/api/v1/orgs");
    expect(posts).toHaveLength(2);
    for (const p of posts) {
      const h = p.init.headers as Record<string, string>;
      expect(h["X-CSRFToken"]).toBe("tok-1");
      expect(h["Content-Type"]).toBe("application/json");
      expect(p.init.credentials).toBe("include");
    }
    expect(JSON.parse(posts[0].init.body as string)).toEqual({ name: "Acme" });
  });

  it("does not send the CSRF header (or fetch a token) on GET", async () => {
    const fetchMock = vi.fn(async () => json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    await api.get("/me");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CSRFToken"]).toBeUndefined();
  });

  it("deduplicates concurrent csrf bootstraps", async () => {
    let csrfHits = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/v1/auth/csrf") {
          csrfHits++;
          await new Promise((r) => setTimeout(r, 5));
          setCookie("csrftoken=tok-2");
          return new Response(null, { status: 204 });
        }
        return new Response(null, { status: 204 });
      }),
    );
    const [a, b] = await Promise.all([ensureCsrf(), ensureCsrf()]);
    expect(csrfHits).toBe(1);
    expect(a).toBe("tok-2");
    expect(b).toBe("tok-2");
  });

  it("refreshes the token and retries once when Django rejects the CSRF token", async () => {
    setCookie("csrftoken=stale");
    let attempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        if (url === "/api/v1/auth/csrf") {
          setCookie("csrftoken=fresh");
          return new Response(null, { status: 204 });
        }
        attempts++;
        const token = (init.headers as Record<string, string>)["X-CSRFToken"];
        if (token === "stale") {
          return json(
            { status: 403, code: "permission_denied", detail: "CSRF Failed: CSRF token incorrect." },
            { status: 403, contentType: "application/problem+json" },
          );
        }
        return new Response(null, { status: 204 });
      }),
    );
    await expect(api.post("/auth/logout")).resolves.toBeUndefined();
    expect(attempts).toBe(2);
  });
});

describe("problem+json parsing", () => {
  it("parses RFC 7807 bodies into ApiError with field errors and request id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(
          {
            type: "about:blank",
            title: "Bad Request",
            status: 400,
            code: "invalid",
            detail: "Validation failed.",
            request_id: "req-42",
            errors: [
              { field: "email", code: "invalid", message: "Enter a valid email address." },
              { field: "email", code: "unique", message: "Already taken." },
            ],
          },
          { status: 400, contentType: "application/problem+json" },
        ),
      ),
    );
    setCookie("csrftoken=t");
    const err = (await api.post("/auth/signup", {}).catch((e: ApiError) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(400);
    expect(err.code).toBe("invalid");
    expect(err.detail).toBe("Validation failed.");
    expect(err.requestId).toBe("req-42");
    expect(err.errors).toHaveLength(2);
    expect(err.fieldErrors()).toEqual({ email: "Enter a valid email address." });
    expect(errorMessage(err)).toContain("email: Enter a valid email address.");
  });

  it("maps domain codes to catalog copy", async () => {
    const err = await toApiError(
      json(
        { status: 403, code: "ai_policy_blocked", detail: null, request_id: "r1" },
        { status: 403, contentType: "application/problem+json" },
      ),
    );
    const d = describeError(err);
    expect(d.code).toBe("ai_policy_blocked");
    expect(d.title).toMatch(/AI data policy/);
    expect(d.requestId).toBe("r1");
  });

  it("handles non-JSON error responses (e.g. a proxy 502) with a status-derived code", async () => {
    const err = await toApiError(new Response("<html>Bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    expect(err.status).toBe(502);
    expect(err.code).toBe("service_unavailable");
  });

  it("turns network failures into status 0 / network_error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const err = (await api.get("/me").catch((e: ApiError) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    expect(err.code).toBe("network_error");
    expect(err.isNetwork).toBe(true);
  });

  it("notifies unauthorized listeners on 401 unless silenced", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: 401, code: "not_authenticated" }, { status: 401 })));
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await api.get("/orgs/acme").catch(() => undefined);
    expect(listener).toHaveBeenCalledTimes(1);
    await api.get("/me", { silent401: true }).catch(() => undefined);
    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });

  it("returns undefined for 204 and parsed JSON otherwise", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    await expect(api.get("/x")).resolves.toBeUndefined();
    vi.stubGlobal("fetch", vi.fn(async () => json({ a: 1 })));
    await expect(api.get("/x")).resolves.toEqual({ a: 1 });
  });

  it("flags not-deployed endpoints", () => {
    expect(isUnavailableEndpoint(new ApiError({ status: 404, code: "not_found" }))).toBe(true);
    expect(isUnavailableEndpoint(new ApiError({ status: 500, code: "server_error" }))).toBe(false);
  });
});

describe("list helpers", () => {
  it("unwraps bare arrays and cursor envelopes", () => {
    expect(unwrapList([1, 2])).toEqual([1, 2]);
    expect(unwrapList({ items: [3], next_cursor: null, previous_cursor: null })).toEqual([3]);
    expect(unwrapList(undefined)).toEqual([]);
  });

  it("extracts the opaque cursor from absolute next URLs", () => {
    expect(cursorFrom("http://backend:8000/api/v1/orgs/acme/audit-events?cursor=cD0yMDI2&limit=100")).toBe("cD0yMDI2");
    expect(cursorFrom(null)).toBeNull();
  });
});
