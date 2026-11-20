import type { FullConfig } from "@playwright/test";

/** Fails fast (or marks the run as skipped with E2E_SKIP_IF_DOWN=1) when the stack is not reachable. */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use?.baseURL ?? "http://localhost:3000";
  let reason = "";
  try {
    const res = await fetch(new URL("/healthz", baseURL), { signal: AbortSignal.timeout(5_000) });
    if (!res.ok) reason = `GET /healthz returned ${res.status}`;
  } catch (err) {
    reason = `stack not reachable at ${baseURL}: ${(err as Error).message}`;
  }
  if (!reason) return;
  if (process.env.E2E_SKIP_IF_DOWN === "1") {
    process.env.E2E_STACK_DOWN = reason;
    console.warn(`[e2e] ${reason} -- tests will be skipped (E2E_SKIP_IF_DOWN=1)`);
    return;
  }
  throw new Error(`[e2e] ${reason}. Start the backend (manage.py runserver) and the frontend (npm run dev), or set E2E_SKIP_IF_DOWN=1.`);
}
