import { type APIRequestContext, type Page } from "@playwright/test";

import { apiIngestCsv, apiSignup, csrfToken, expect, makeUser, SALES_CSV, test } from "./fixtures";

/**
 * W4 governance + platform console (CONTRACT "W4 additions"; API_SPECIFICATION Phase 5 and Phase 11):
 *  1. Governance: create a column MASK in /o/<slug>/governance (UI; API fallback recorded as not-exercised), then the
 *     policy preview (UI and API) shows the column masked for that member.
 *  2. Platform console gating: a normal user gets the 404 page at /platform and 404 from /api/v1/platform/*;
 *     a platform admin (E2E_PLATFORM_EMAIL / E2E_PLATFORM_PASSWORD, created with `manage.py create_platform_admin`)
 *     sees the orgs table. Without those variables the admin half is recorded as not-exercised.
 */

function notExercised(description: string): void {
  test.info().annotations.push({ type: "not-exercised", description });
}

async function me(request: APIRequestContext): Promise<{ id: string; is_platform_admin: boolean }> {
  const res = await request.get("/api/v1/me");
  expect(res.status(), await res.text()).toBe(200);
  const body = await res.json();
  return body.user ?? body;
}

async function apiLogin(request: APIRequestContext, email: string, password: string): Promise<void> {
  const res = await request.post("/api/v1/auth/login", {
    headers: { "X-CSRFToken": await csrfToken(request) },
    data: { email, password },
  });
  expect(res.status(), await res.text()).toBeLessThan(300);
}

async function createMaskInUi(page: Page, slug: string, column: string, email: string): Promise<boolean> {
  await page.goto(`/o/${slug}/governance`);
  const tab = page.getByRole("tab", { name: /column polic/i });
  if (!(await tab.isVisible({ timeout: 15_000 }).catch(() => false))) return false;
  await tab.click();
  const add = page.getByRole("button", { name: /add|new/i }).first();
  if (!(await add.isVisible())) return false;
  await add.click();
  const dialog = page.getByRole("dialog");
  for (const [label, option] of [
    [/dataset/i, /E2E Sales/],
    [/column/i, new RegExp(`^${column}$`)],
    [/user|principal|member/i, new RegExp(email.replace(/[.+]/g, "\\$&"))],
    [/action|rule/i, /mask/i],
    [/mask type|mask kind/i, /null|hash/i],
  ] as const) {
    const box = dialog.getByRole("combobox", { name: label }).first();
    if (!(await box.isVisible())) continue;
    await box.click();
    const choice = page.getByRole("option", { name: option }).first();
    if (!(await choice.isVisible())) return false;
    await choice.click();
  }
  await dialog.getByRole("button", { name: /save|create|add/i }).last().click();
  await expect(dialog).toBeHidden({ timeout: 30_000 });
  return true;
}

test.describe("W4: governance", () => {
  test("a column mask shows as masked in the policy preview", async ({ page, user }) => {
    const slug = await apiSignup(page.request, user);
    const datasetId = await apiIngestCsv(page.request, slug, SALES_CSV, "E2E Sales");
    const self = await me(page.request);
    const base = `/api/v1/orgs/${slug}/datasets/${datasetId}`;
    const headers = { "X-CSRFToken": await csrfToken(page.request) };

    if (!(await createMaskInUi(page, slug, "customer_name", user.email))) {
      notExercised("governance UI: column-policy form not drivable by role/label; mask created through the API");
      const res = await page.request.post(`${base}/column-policies`, {
        headers,
        data: { column_name: "customer_name", principal_type: "user", principal_id: self.id, action: "mask_hash" },
      });
      expect(res.status(), await res.text()).toBe(201);
    }

    const preview = await page.request.post(`${base}/policy-preview`, { headers, data: { user_id: self.id } });
    expect(preview.status(), await preview.text()).toBe(200);
    const body = await preview.json();
    expect(body.access).toBe(true);
    const policy = JSON.stringify(body.policy);
    expect(policy).toContain("customer_name");
    expect(policy).toMatch(/mask/i);

    // the UI preview shows the same (member + dataset picker -> masked column)
    await page.goto(`/o/${slug}/governance`);
    const previewTab = page.getByRole("tab", { name: /preview/i });
    if (!(await previewTab.isVisible({ timeout: 15_000 }).catch(() => false))) {
      notExercised("governance UI: no Policy preview tab");
      return;
    }
    await previewTab.click();
    for (const [label, option] of [
      [/user|member/i, new RegExp(user.email.replace(/[.+]/g, "\\$&"))],
      [/dataset/i, /E2E Sales/],
    ] as const) {
      const box = page.getByRole("combobox", { name: label }).first();
      if (await box.isVisible()) {
        await box.click();
        await page.getByRole("option", { name: option }).first().click();
      }
    }
    const run = page.getByRole("button", { name: /preview/i }).last();
    if (await run.isVisible()) await run.click();
    const row = page.getByRole("row", { name: /customer_name/ }).first();
    await expect(row).toBeVisible({ timeout: 30_000 });
    await expect(row).toContainText(/mask/i);
  });
});

test.describe("W4: platform console gating", () => {
  test("a normal user gets 404 at /platform and on every platform API", async ({ page, user }) => {
    await apiSignup(page.request, user);
    expect((await me(page.request)).is_platform_admin).toBe(false);
    for (const api of ["/api/v1/platform/orgs", "/api/v1/platform/health", "/api/v1/platform/jobs",
      "/api/v1/platform/ai/usage", "/api/v1/platform/logs"]) {
      expect((await page.request.get(api)).status(), api).toBe(404);
    }
    await page.goto("/platform");
    await expect(page.getByText(/404|not found|page could not be found/i).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("table", { name: /organi[sz]ations|orgs/i })).toHaveCount(0);
  });

  test("a platform admin sees the orgs table", async ({ page }) => {
    const email = process.env.E2E_PLATFORM_EMAIL;
    const password = process.env.E2E_PLATFORM_PASSWORD;
    if (!email || !password) {
      notExercised("platform admin: set E2E_PLATFORM_EMAIL / E2E_PLATFORM_PASSWORD (manage.py create_platform_admin)");
      return;
    }
    // make sure at least one org exists besides the admin's own
    await apiSignup(page.context().request, makeUser("platformtarget"));
    await page.context().clearCookies();
    await apiLogin(page.request, email, password);
    expect((await me(page.request)).is_platform_admin).toBe(true);
    const orgs = await page.request.get("/api/v1/platform/orgs");
    expect(orgs.status(), await orgs.text()).toBe(200);
    await page.goto("/platform");
    await expect(page.getByRole("table").first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("button", { name: /suspend/i }).first()).toBeVisible();
  });
});
