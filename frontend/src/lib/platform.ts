/**
 * Platform console helpers (API_SPECIFICATION Phase 11, `apps/platform_admin`). Pure; unit-tested.
 *
 * Every `/api/v1/platform/*` route answers 404 to anyone who is not `User.is_platform_admin` (the console must not
 * even reveal that it exists), so the UI gates on `/me` and renders the regular 404 page otherwise.
 */

import type { MeResponse } from "@/lib/types";

export type PlatformGate = "loading" | "signed_out" | "not_found" | "allowed";

/** `me` is undefined while loading and null when signed out (useMe contract). */
export function platformGate(me: MeResponse | null | undefined, isLoading: boolean): PlatformGate {
  if (me === undefined) return isLoading ? "loading" : "not_found";
  if (me === null) return "signed_out";
  return me.user?.is_platform_admin === true ? "allowed" : "not_found";
}

export type Tone = "success" | "warning" | "danger" | "neutral" | "info";

/** Health component / overall status -> badge tone. */
export function healthTone(status: string | undefined | null): Tone {
  switch (status) {
    case "ok":
    case "yes":
    case "closed":
      return "success";
    case "degraded":
    case "stale":
    case "unknown":
    case "half_open":
    case "unavailable":
    case "not_configured":
      return "warning";
    case "error":
    case "failing":
    case "open":
      return "danger";
    default:
      return "neutral";
  }
}

export function orgStatusTone(status: string): Tone {
  return status === "active" ? "success" : status === "suspended" ? "warning" : status === "deleted" ? "danger" : "neutral";
}

export const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];

export function logTone(level: string): Tone {
  return level === "ERROR" || level === "CRITICAL" ? "danger" : level === "WARNING" ? "warning" : level === "DEBUG" ? "neutral" : "info";
}

/** Mirrors the platform log filter's `component` rule (`^[A-Za-z0-9_.-]{1,120}$`). */
export const COMPONENT_RE = /^[A-Za-z0-9_.-]{1,120}$/;

/* ---------------- model quota form ---------------- */

export interface QuotaDraft {
  rpm: string;
  tpm: string;
  rpd: string;
  notes: string;
}

export interface QuotaBody {
  rpm: number;
  tpm: number;
  rpd: number;
  notes: string;
  verified: boolean;
}

const LIMITS = { rpm: 1_000_000, tpm: 1_000_000_000, rpd: 100_000_000 } as const;

/** Validate a quota draft against the server's ModelQuotaBody bounds. Returns field errors or the body. */
export function parseQuota(d: QuotaDraft): { errors: Partial<Record<keyof QuotaDraft, string>>; body: QuotaBody | null } {
  const errors: Partial<Record<keyof QuotaDraft, string>> = {};
  const nums: Record<string, number> = {};
  for (const k of ["rpm", "tpm", "rpd"] as const) {
    const t = d[k].trim().replace(/[,_\s]/g, "");
    const n = Number(t);
    if (!t) errors[k] = "Required.";
    else if (!/^\d+$/.test(t) || !Number.isSafeInteger(n)) errors[k] = "Whole number.";
    else if (n < 1 || n > LIMITS[k]) errors[k] = `1 to ${LIMITS[k].toLocaleString("en-US")}.`;
    else nums[k] = n;
  }
  if (d.notes.length > 200) errors.notes = "At most 200 characters.";
  if (Object.keys(errors).length) return { errors, body: null };
  return { errors, body: { rpm: nums.rpm, tpm: nums.tpm, rpd: nums.rpd, notes: d.notes.trim(), verified: true } };
}

/** Requests per day per model -> chart rows `{day, <model>: n}`. */
export function usageByDayAndModel(daily: Array<{ day: string; model: string; requests: number }>): { rows: Array<Record<string, string | number>>; models: string[] } {
  const models = Array.from(new Set(daily.map((d) => d.model))).sort();
  const byDay = new Map<string, Record<string, string | number>>();
  for (const d of daily) {
    const row = byDay.get(d.day) ?? Object.fromEntries([["day", d.day], ...models.map((m) => [m, 0])]);
    row[d.model] = (Number(row[d.model]) || 0) + d.requests;
    byDay.set(d.day, row);
  }
  return { rows: Array.from(byDay.values()).sort((a, b) => String(a.day).localeCompare(String(b.day))), models };
}
