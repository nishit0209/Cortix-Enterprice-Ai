/**
 * Typed fetch wrapper for the Cortix API (contracts/api/CONTRACT.md).
 *
 * - Same-origin requests (`/api/v1/...` is rewritten to the Django backend by next.config.ts).
 * - Session cookie auth: `credentials: "include"`.
 * - CSRF: GET /api/v1/auth/csrf once to obtain the `csrftoken` cookie, then send `X-CSRFToken` on unsafe methods.
 * - Errors: application/problem+json is parsed into `ApiError { status, code, detail, errors, requestId }`.
 */

import type { Paginated, ProblemDetails, ProblemFieldError } from "./types";

export const API_BASE = "/api/v1";
export const CSRF_COOKIE = "csrftoken";
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string | null;
  readonly errors: ProblemFieldError[];
  readonly requestId: string | null;
  readonly body: ProblemDetails | null;

  constructor(init: {
    status: number;
    code: string;
    detail?: string | null;
    errors?: ProblemFieldError[];
    requestId?: string | null;
    body?: ProblemDetails | null;
    title?: string;
  }) {
    super(init.detail || init.title || init.code);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.detail = init.detail ?? null;
    this.errors = init.errors ?? [];
    this.requestId = init.requestId ?? null;
    this.body = init.body ?? null;
  }

  /** Field-level messages keyed by field name (for react-hook-form setError). */
  fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const e of this.errors) {
      const key = e.field ?? "_";
      if (!out[key]) out[key] = e.message;
    }
    return out;
  }

  get isNotFound() {
    return this.status === 404;
  }

  get isUnauthorized() {
    return this.status === 401;
  }

  get isNetwork() {
    return this.status === 0;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/* ---------------- cookies & CSRF ---------------- */

export function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const parts = document.cookie ? document.cookie.split(";") : [];
  for (const part of parts) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

let csrfInFlight: Promise<void> | null = null;

/** Ensure the csrftoken cookie exists. Calls GET /auth/csrf at most once concurrently. */
export async function ensureCsrf(force = false): Promise<string | null> {
  if (!force) {
    const existing = readCookie(CSRF_COOKIE);
    if (existing) return existing;
  }
  if (!csrfInFlight || force) {
    csrfInFlight = fetch(`${API_BASE}/auth/csrf`, { method: "GET", credentials: "include" })
      .then(() => undefined)
      .catch(() => undefined)
      .finally(() => {
        csrfInFlight = null;
      });
  }
  await csrfInFlight;
  return readCookie(CSRF_COOKIE);
}

/** Test hook: reset module-level CSRF state. */
export function __resetCsrfForTests() {
  csrfInFlight = null;
}

/* ---------------- unauthorized listeners ---------------- */

type Listener = (err: ApiError) => void;
const unauthorizedListeners = new Set<Listener>();

export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

/* ---------------- request ---------------- */

export type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Query;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Do not notify unauthorized listeners on 401 (e.g. GET /me probes, login form). */
  silent401?: boolean;
}

export function buildUrl(path: string, query?: Query): string {
  let url = path;
  if (!/^https?:\/\//.test(path) && !path.startsWith(API_BASE) && !path.startsWith("/healthz")) {
    url = `${API_BASE}${path.startsWith("/") ? "" : "/"}${path}`;
  }
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === "") continue;
      qs.set(k, String(v));
    }
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }
  return url;
}

function isJsonContentType(ct: string | null): boolean {
  return !!ct && (ct.includes("application/json") || ct.includes("+json"));
}

/** Parse any failed Response into an ApiError (problem+json when available). */
export async function toApiError(res: Response): Promise<ApiError> {
  const ct = res.headers.get("content-type");
  let body: unknown = null;
  if (isJsonContentType(ct)) {
    try {
      body = await res.json();
    } catch {
      body = null;
    }
  }
  if (body && typeof body === "object") {
    const p = body as Partial<ProblemDetails> & { detail?: unknown };
    const detail =
      typeof p.detail === "string" ? p.detail : p.detail == null ? null : JSON.stringify(p.detail);
    return new ApiError({
      status: typeof p.status === "number" ? p.status : res.status,
      code: typeof p.code === "string" && p.code ? p.code : defaultCodeFor(res.status),
      detail,
      title: typeof p.title === "string" ? p.title : undefined,
      errors: Array.isArray(p.errors) ? (p.errors as ProblemFieldError[]) : [],
      requestId: typeof p.request_id === "string" ? p.request_id : res.headers.get("x-request-id"),
      body: { ...(p as ProblemDetails), status: res.status },
    });
  }
  return new ApiError({
    status: res.status,
    code: defaultCodeFor(res.status),
    detail: res.statusText || null,
    requestId: res.headers.get("x-request-id"),
  });
}

function defaultCodeFor(status: number): string {
  if (status === 401) return "not_authenticated";
  if (status === 403) return "permission_denied";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 429) return "rate_limited";
  if (status === 502 || status === 503 || status === 504) return "service_unavailable";
  if (status >= 500) return "server_error";
  return "error";
}

function looksLikeCsrfFailure(err: ApiError): boolean {
  return err.status === 403 && /csrf/i.test(err.detail ?? "");
}

export async function apiFetch<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  return doFetch<T>(path, opts, false);
}

async function doFetch<T>(path: string, opts: RequestOptions, isRetry: boolean): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json", ...(opts.headers ?? {}) };
  let body: BodyInit | undefined;

  if (opts.body !== undefined && opts.body !== null) {
    if (typeof FormData !== "undefined" && opts.body instanceof FormData) {
      body = opts.body;
    } else {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
  }

  if (UNSAFE.has(method)) {
    const token = await ensureCsrf(isRetry);
    if (token) headers["X-CSRFToken"] = token;
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      credentials: "include",
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e;
    throw new ApiError({
      status: 0,
      code: "network_error",
      detail: "Could not reach the Cortix API. Check your connection or try again shortly.",
    });
  }

  if (!res.ok) {
    const err = await toApiError(res);
    if (!isRetry && UNSAFE.has(method) && looksLikeCsrfFailure(err)) {
      return doFetch<T>(path, opts, true);
    }
    if (err.status === 401 && !opts.silent401) {
      unauthorizedListeners.forEach((l) => l(err));
    }
    throw err;
  }

  if (res.status === 204 || res.status === 205) return undefined as T;
  const ct = res.headers.get("content-type");
  if (isJsonContentType(ct)) {
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }
  return (await res.text()) as unknown as T;
}

export const api = {
  get: <T>(path: string, opts?: Omit<RequestOptions, "method" | "body">) => apiFetch<T>(path, { ...opts, method: "GET" }),
  post: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiFetch<T>(path, { ...opts, method: "POST", body }),
  patch: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiFetch<T>(path, { ...opts, method: "PATCH", body }),
  put: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiFetch<T>(path, { ...opts, method: "PUT", body }),
  delete: <T = void>(path: string, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiFetch<T>(path, { ...opts, method: "DELETE" }),
};

/* ---------------- list helpers ---------------- */

/** Accepts either a bare array or the `{items, next_cursor, previous_cursor}` envelope. */
export function unwrapList<T>(data: T[] | Paginated<T> | null | undefined): T[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  if (Array.isArray((data as Paginated<T>).items)) return (data as Paginated<T>).items;
  return [];
}

/** The API returns absolute cursor URLs; we only forward the opaque `cursor` param through our own proxy. */
export function cursorFrom(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url, "http://local");
    return u.searchParams.get("cursor");
  } catch {
    return null;
  }
}

/**
 * GET every page of a cursor-paginated list (following `next_cursor`), up to `maxItems`.
 * Accepts bare arrays too (returned as-is).
 */
export async function fetchAll<T>(
  path: string,
  { query, signal, pageSize = 200, maxItems = 2000 }: { query?: Query; signal?: AbortSignal; pageSize?: number; maxItems?: number } = {},
): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | null = null;
  for (let guard = 0; guard < 100; guard++) {
    const res: T[] | Paginated<T> = await api.get<T[] | Paginated<T>>(path, { signal, query: { ...query, limit: pageSize, cursor: cursor ?? undefined } });
    if (Array.isArray(res)) return res.slice(0, maxItems);
    out.push(...(res.items ?? []));
    cursor = cursorFrom(res.next_cursor);
    if (!cursor || out.length >= maxItems) break;
  }
  return out.slice(0, maxItems);
}

/* ---------------- uploads with progress ---------------- */

export interface UploadProgress {
  loaded: number;
  total: number;
  percent: number;
}

/** multipart upload via XHR so the UI can show byte progress (fetch has no upload progress). */
export async function uploadWithProgress<T>(
  path: string,
  form: FormData,
  onProgress?: (p: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<T> {
  const token = await ensureCsrf();
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", buildUrl(path));
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");
    if (token) xhr.setRequestHeader("X-CSRFToken", token);
    xhr.upload.onprogress = (ev) => {
      if (!onProgress || !ev.lengthComputable) return;
      onProgress({ loaded: ev.loaded, total: ev.total, percent: Math.round((ev.loaded / ev.total) * 100) });
    };
    xhr.onerror = () =>
      reject(new ApiError({ status: 0, code: "network_error", detail: "Upload failed: the API is unreachable." }));
    xhr.onabort = () => reject(new DOMException("Upload aborted", "AbortError"));
    xhr.onload = () => {
      const res = new Response(xhr.status === 204 ? null : xhr.responseText, {
        status: xhr.status,
        statusText: xhr.statusText,
        headers: { "content-type": xhr.getResponseHeader("content-type") ?? "" },
      });
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve((xhr.responseText ? JSON.parse(xhr.responseText) : undefined) as T);
        } catch {
          resolve(undefined as T);
        }
      } else {
        toApiError(res).then(reject, reject);
      }
    };
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(form);
  });
}
