/**
 * File downloads.
 *
 * - Exports (`POST /exports`) answer either with the file stream itself (Content-Disposition attachment) or with
 *   JSON pointing at a URL (`{download_url|url, filename?}`).
 * - On the local runtime that URL is a signed, expiring `/api/v1/files/<token>`; on S3 it is a presigned URL.
 *   An expired link answers 403 (S3) or 410 `download_expired` (local files view). Both are reported as the
 *   `download_expired` error, and `postDownload` transparently asks for a fresh link once.
 */

import { ApiError, buildUrl, ensureCsrf, toApiError } from "./api";

export const DOWNLOAD_EXPIRED = "download_expired";

function filenameFrom(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1].trim() : fallback;
}

export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

/** Only same-origin or https URLs returned by the API are followed (no javascript:/data: links). */
export function isSafeDownloadUrl(url: string): boolean {
  try {
    const u = new URL(url, window.location.origin);
    return u.protocol === "https:" || u.origin === window.location.origin;
  } catch {
    return false;
  }
}

/** True for the error a stale signed/presigned link produces. */
export function isExpiredDownload(e: unknown): boolean {
  return e instanceof ApiError && e.code === DOWNLOAD_EXPIRED;
}

/**
 * Fetch a signed URL and save it. Same-origin URLs (`/api/v1/files/...`) are fetched so an expired link can be
 * detected and explained; cross-origin presigned URLs (S3) are opened directly by the browser.
 */
export async function downloadSignedUrl(url: string, fallbackName: string, fetchImpl: typeof fetch = fetch): Promise<{ filename: string }> {
  if (!isSafeDownloadUrl(url)) {
    throw new ApiError({ status: 0, code: "export_unavailable", detail: "The server returned an unsafe download link." });
  }
  const sameOrigin = new URL(url, window.location.origin).origin === window.location.origin;
  if (!sameOrigin) {
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    a.download = fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    return { filename: fallbackName };
  }
  let res: Response;
  try {
    res = await fetchImpl(url, { method: "GET", credentials: "include" });
  } catch {
    throw new ApiError({ status: 0, code: "network_error", detail: "Could not reach the NEXORA API." });
  }
  if (res.status === 403 || res.status === 410) {
    const err = await toApiError(res);
    throw new ApiError({
      status: res.status,
      code: DOWNLOAD_EXPIRED,
      detail: err.code === DOWNLOAD_EXPIRED || res.status === 410 ? (err.detail ?? "This download link has expired.") : "This download link has expired or is no longer valid.",
      requestId: err.requestId,
    });
  }
  if (!res.ok) throw await toApiError(res);
  const filename = filenameFrom(res.headers.get("content-disposition"), fallbackName);
  saveBlob(await res.blob(), filename);
  return { filename };
}

async function postOnce(path: string, body: unknown, fetchImpl: typeof fetch): Promise<Response> {
  const token = await ensureCsrf();
  try {
    return await fetchImpl(buildUrl(path), {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/octet-stream, application/json;q=0.9, */*;q=0.8",
        ...(token ? { "X-CSRFToken": token } : {}),
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError({ status: 0, code: "network_error", detail: "Could not reach the NEXORA API." });
  }
}

export async function postDownload(path: string, body: unknown, fallbackName: string, fetchImpl: typeof fetch = fetch): Promise<{ filename: string }> {
  for (let attempt = 0; attempt < 2; attempt++) {
    const res = await postOnce(path, body, fetchImpl);
    if (!res.ok) throw await toApiError(res);
    const ct = res.headers.get("content-type") ?? "";
    if (!ct.includes("application/json")) {
      const filename = filenameFrom(res.headers.get("content-disposition"), fallbackName);
      saveBlob(await res.blob(), filename);
      return { filename };
    }
    const data = (await res.json()) as { download_url?: string; url?: string; filename?: string };
    const url = data.download_url ?? data.url;
    if (!url) throw new ApiError({ status: 0, code: "export_unavailable", detail: "The export did not return a downloadable file." });
    try {
      return await downloadSignedUrl(url, data.filename ?? fallbackName, fetchImpl);
    } catch (e) {
      // A link that expired between issue and use: ask for a fresh one once.
      if (attempt === 0 && isExpiredDownload(e)) continue;
      throw e;
    }
  }
  throw new ApiError({ status: 410, code: DOWNLOAD_EXPIRED, detail: "The download link expired. Try again." });
}
