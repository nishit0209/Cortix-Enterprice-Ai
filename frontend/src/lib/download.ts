/**
 * File downloads from authenticated POST endpoints (exports). The response is either the file stream itself
 * (Content-Disposition attachment) or JSON pointing at a pre-signed URL ({download_url|url}).
 */

import { ApiError, buildUrl, ensureCsrf, toApiError } from "./api";

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

export async function postDownload(path: string, body: unknown, fallbackName: string): Promise<{ filename: string }> {
  const token = await ensureCsrf();
  let res: Response;
  try {
    res = await fetch(buildUrl(path), {
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
  if (!res.ok) throw await toApiError(res);
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    const data = (await res.json()) as { download_url?: string; url?: string; filename?: string };
    const url = data.download_url ?? data.url;
    if (!url || !isSafeDownloadUrl(url)) {
      throw new ApiError({ status: 0, code: "export_unavailable", detail: "The export did not return a downloadable file." });
    }
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    a.download = data.filename ?? fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    return { filename: data.filename ?? fallbackName };
  }
  const filename = filenameFrom(res.headers.get("content-disposition"), fallbackName);
  saveBlob(await res.blob(), filename);
  return { filename };
}
