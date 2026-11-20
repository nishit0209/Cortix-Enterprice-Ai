import { type APIRequestContext, type Page, test as base, expect } from "@playwright/test";
import path from "node:path";

/** Synthetic identity for one test run (reserved example.test domain; never a real person). */
export interface TestUser {
  email: string;
  password: string;
  fullName: string;
  orgName: string;
}

export const TESTDATA_DIR = path.resolve(__dirname, "..", "..", "testdata", "generated");
export const SALES_CSV = path.join(TESTDATA_DIR, "sales.csv");

export function makeUser(tag = "e2e"): TestUser {
  const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  return {
    email: `${tag}-${id}@example.test`,
    password: `Nx-synthetic-${id}-Pa55!`,
    fullName: `E2E ${tag} ${id}`,
    orgName: `E2E Org ${id}`,
  };
}

/** Sign up through the public API exactly like the SPA (CSRF cookie + X-CSRFToken). Returns the org slug. */
export async function apiSignup(request: APIRequestContext, user: TestUser): Promise<string> {
  const csrf = await request.get("/api/v1/auth/csrf");
  expect(csrf.status(), "GET /api/v1/auth/csrf").toBeLessThan(300);
  const state = await request.storageState();
  const token = state.cookies.find((c) => c.name === "csrftoken")?.value ?? "";
  const res = await request.post("/api/v1/auth/signup", {
    headers: { "X-CSRFToken": token },
    data: { email: user.email, password: user.password, full_name: user.fullName, org_name: user.orgName },
  });
  expect(res.status(), await res.text()).toBe(201);
  const body = await res.json();
  return body.org.slug as string;
}

/**
 * Sign up through the UI (frontend/src/components/auth/signup-form.tsx: Field labels "Full name", "Work email",
 * "Organization name", "Password"; submit "Create workspace"; redirect to /o/<slug>/dashboard).
 * Exact label matches: the "Password requirements" list and the show/hide toggle are also labelled.
 * Returns the org slug parsed from the URL.
 */
export async function uiSignup(page: Page, user: TestUser): Promise<string> {
  await page.goto("/signup");
  await page.getByLabel("Full name", { exact: true }).fill(user.fullName);
  await page.getByLabel("Work email", { exact: true }).fill(user.email);
  await page.getByLabel("Organization name", { exact: true }).fill(user.orgName);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "Create workspace" }).click();
  await page.waitForURL(/\/o\/[a-z0-9-]+\/dashboard$/, { timeout: 30_000 });
  const match = /\/o\/([a-z0-9-]+)/.exec(new URL(page.url()).pathname);
  expect(match, `unexpected post-signup URL ${page.url()}`).not.toBeNull();
  return match![1];
}

export const test = base.extend<{ user: TestUser; stackUp: void }>({
  stackUp: [
    async ({}, use, testInfo) => {
      testInfo.skip(!!process.env.E2E_STACK_DOWN, process.env.E2E_STACK_DOWN);
      await use();
    },
    { auto: true },
  ],
  user: async ({}, use, testInfo) => {
    await use(makeUser(testInfo.project.name.replace(/[^a-z0-9]/gi, "").toLowerCase()));
  },
});

export { expect };

/** The csrftoken cookie of a request context (page.request shares the page's cookies). */
export async function csrfToken(request: APIRequestContext): Promise<string> {
  let token = (await request.storageState()).cookies.find((c) => c.name === "csrftoken")?.value ?? "";
  if (!token) {
    await request.get("/api/v1/auth/csrf");
    token = (await request.storageState()).cookies.find((c) => c.name === "csrftoken")?.value ?? "";
  }
  return token;
}

/** List payload of a JSON response: the `items` envelope or a bare array. */
export function itemsOf<T = Record<string, unknown>>(body: unknown): T[] {
  if (Array.isArray(body)) return body as T[];
  const env = body as { items?: T[] } | null;
  return env?.items ?? [];
}

/**
 * Upload a synthetic CSV and ingest it through the PUBLIC API (POST /uploads multipart, POST /datasets with
 * classification "synthetic" + attestation), then wait until the dataset is ready. Returns the dataset id.
 */
export async function apiIngestCsv(
  request: APIRequestContext,
  slug: string,
  file: string,
  name: string,
  timeoutMs = 180_000,
): Promise<string> {
  const base = `/api/v1/orgs/${slug}`;
  const headers = { "X-CSRFToken": await csrfToken(request) };
  const fs = await import("node:fs");
  const up = await request.post(`${base}/uploads`, {
    headers,
    multipart: { file: { name: path.basename(file), mimeType: "text/csv", buffer: fs.readFileSync(file) } },
  });
  expect(up.status(), await up.text()).toBe(201);
  const created = await request.post(`${base}/datasets`, {
    headers,
    data: { name, upload_id: (await up.json()).id, classification: "synthetic", attest: true },
  });
  expect(created.status(), await created.text()).toBe(202);
  const datasetId = (await created.json()).dataset.id as string;
  await expect
    .poll(async () => (await (await request.get(`${base}/datasets/${datasetId}`)).json()).status, {
      timeout: timeoutMs,
      intervals: [1_000, 2_000, 5_000],
    })
    .toBe("ready");
  return datasetId;
}

/** Unread notification count from the API (`GET /notifications?unread=1`); null when the route is absent. */
export async function apiUnreadCount(request: APIRequestContext, slug: string): Promise<number | null> {
  const res = await request.get(`/api/v1/orgs/${slug}/notifications?unread=1`);
  if (res.status() === 404) return null;
  expect(res.status(), await res.text()).toBe(200);
  const body = await res.json();
  if (typeof body?.unread_count === "number") return body.unread_count as number;
  return itemsOf(body).length;
}

export const REGIONS = ["North", "South", "East", "West", "Central"];

/**
 * Ask AI through the labelled composer (frontend W3: `<textarea aria-label="Ask a question">`), submitting with
 * Enter like a user. The composer is located by its accessible name, never by CSS.
 */
export async function askQuestion(page: Page, question: string): Promise<void> {
  const box = page.getByRole("textbox", { name: "Ask a question", exact: true });
  await expect(box).toBeVisible({ timeout: 30_000 });
  await box.fill(question);
  await box.press("Enter");
}

/** The answer renders its SQL and a result containing the 5 sales regions (deterministic builder path). */
export async function expectSqlAndRegions(page: Page): Promise<void> {
  await expect(page.getByText(/\bselect\b[\s\S]*\bfrom\b/i).first()).toBeVisible({ timeout: 90_000 });
  const table = page.getByRole("table").last();
  await expect(table).toBeVisible({ timeout: 30_000 });
  for (const region of REGIONS) {
    await expect(table.getByRole("cell", { name: region, exact: true }).first()).toBeVisible();
  }
}

/** True when /o/<slug>/ask still renders the honest Planned placeholder (nothing to drive yet). */
export async function askIsPlanned(page: Page): Promise<boolean> {
  return page.getByText("Ask AI is on the roadmap").isVisible();
}
