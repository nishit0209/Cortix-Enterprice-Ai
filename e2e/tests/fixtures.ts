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

/** Sign up through the UI. Returns the org slug parsed from the /o/<slug>/ URL. */
export async function uiSignup(page: Page, user: TestUser): Promise<string> {
  await page.goto("/signup");
  await page.getByLabel(/full name|your name/i).fill(user.fullName);
  await page.getByLabel(/^email|work email/i).fill(user.email);
  await page.getByLabel(/^password/i).first().fill(user.password);
  await page.getByLabel(/organi[sz]ation|workspace|company/i).fill(user.orgName);
  await page.getByRole("button", { name: /sign up|create (account|workspace)|get started/i }).click();
  await page.waitForURL(/\/o\/[a-z0-9-]+(\/|$)/, { timeout: 30_000 });
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
