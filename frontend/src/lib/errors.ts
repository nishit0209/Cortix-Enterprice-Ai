/**
 * Central catalog: API error `code` -> plain-language message + suggested next step (UI/UX spec §7).
 * Mirrors docs/API_SPECIFICATION.md §3. Unknown codes fall back to the server `detail`.
 */
import { isApiError } from "./api";

export interface ErrorCopy {
  title: string;
  description: string;
  action?: string;
}

export const ERROR_CATALOG: Record<string, ErrorCopy> = {
  network_error: {
    title: "Can't reach NEXORA",
    description: "The API did not respond. The backend may be starting up or your connection dropped.",
    action: "Retry in a few seconds.",
  },
  service_unavailable: {
    title: "Service temporarily unavailable",
    description: "A dependency of this page is not available right now.",
    action: "Retry shortly. If it persists, contact your administrator.",
  },
  server_error: {
    title: "Something went wrong on our side",
    description: "The server hit an unexpected error. It has been logged with the request ID below.",
    action: "Retry, or share the request ID with support.",
  },
  not_authenticated: {
    title: "You're signed out",
    description: "Your session expired or you are not signed in.",
    action: "Sign in again to continue.",
  },
  authentication_failed: {
    title: "Sign-in failed",
    description: "The email or password is incorrect.",
  },
  invalid_credentials: {
    title: "Sign-in failed",
    description: "The email or password is incorrect.",
  },
  permission_denied: {
    title: "You don't have access",
    description: "Your role does not include the permission needed for this action.",
    action: "Ask an organization owner or admin to grant access.",
  },
  not_found: {
    title: "Not found",
    description: "This resource doesn't exist or isn't available in this organization.",
  },
  conflict: {
    title: "Conflict",
    description: "The resource changed or the action conflicts with its current state.",
  },
  last_owner: {
    title: "An organization needs an owner",
    description: "You can't remove or demote the last owner. Promote another member to owner first.",
  },
  rate_limited: {
    title: "Too many requests",
    description: "You're sending requests faster than allowed.",
    action: "Wait a moment and try again.",
  },
  invalid: {
    title: "Please check the form",
    description: "Some fields are invalid.",
  },
  ai_policy_blocked: {
    title: "Blocked by your AI data policy",
    description:
      "Your organization's AI data policy does not allow sending this data to Gemini. Only datasets classified synthetic or public can be used under synthetic_only.",
    action: "Reclassify the dataset (with attestation) or ask an admin to review the AI policy.",
  },
  ai_policy_consent_required: {
    title: "Consent required",
    description: "schema_only requires a valid consent from an owner or admin.",
    action: "Grant consent from Settings → AI policy.",
  },
  ai_policy_tier_forbidden: {
    title: "Not available on the free Gemini tier",
    description: "aggregates_only and full require a paid Gemini tier, because free-tier prompts may be used for training.",
  },
  ai_quota_exhausted: {
    title: "AI quota reached",
    description: "Your organization's daily Gemini budget is used up. Cached and builder answers still work.",
    action: "The budget resets automatically.",
  },
  ai_unavailable: {
    title: "AI is temporarily unavailable",
    description: "Gemini is not reachable or the circuit breaker is open.",
    action: "Non-AI features keep working. Try again later.",
  },
  ai_invalid_output: {
    title: "The AI response could not be used",
    description: "Gemini's output failed validation after a repair attempt.",
    action: "Rephrase the question or use the visual query builder.",
  },
  sql_rejected: {
    title: "SQL rejected by the guard",
    description: "The query violates a security rule (for example multiple statements or a forbidden table).",
  },
  query_timeout: {
    title: "Query timed out",
    description: "The query ran longer than the allowed statement timeout.",
    action: "Add filters or aggregate to reduce the scanned data.",
  },
  upload_rejected: {
    title: "Upload rejected",
    description: "The file is too large, of an unsupported type, or could not be parsed.",
    action: "Upload a CSV, XLSX, JSON or Parquet file within the size limit.",
  },
  connector_not_readonly: {
    title: "Database role is not read-only",
    description: "NEXORA only connects with read-only roles.",
  },
  connector_host_forbidden: {
    title: "Host not allowed",
    description: "The host resolved to a private, loopback or link-local address.",
  },
};

export function describeError(error: unknown): ErrorCopy & { code: string; requestId: string | null } {
  if (isApiError(error)) {
    const known = ERROR_CATALOG[error.code];
    const description =
      known && (!error.detail || error.code === "network_error") ? known.description : error.detail || known?.description || "";
    return {
      code: error.code,
      requestId: error.requestId,
      title: known?.title ?? (error.status >= 500 ? ERROR_CATALOG.server_error.title : "Request failed"),
      description: description || "The request could not be completed.",
      action: known?.action,
    };
  }
  const msg = error instanceof Error ? error.message : "Unexpected error";
  return { code: "client_error", requestId: null, title: "Something went wrong", description: msg };
}

/** One-line message for toasts. */
export function errorMessage(error: unknown): string {
  const d = describeError(error);
  if (isApiError(error) && error.errors.length > 0) {
    return error.errors.map((e) => (e.field ? `${e.field}: ${e.message}` : e.message)).join(" · ");
  }
  return d.description && d.description !== d.title ? `${d.title}: ${d.description}` : d.title;
}

/** True when the endpoint simply isn't deployed yet (wave not merged) — render "not available" rather than a scary error. */
export function isUnavailableEndpoint(error: unknown): boolean {
  return isApiError(error) && (error.status === 404 || error.status === 405 || error.status === 501);
}
