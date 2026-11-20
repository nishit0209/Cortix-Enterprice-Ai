import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { apiSignup, expect, test } from "../fixtures";

/**
 * axe-core on every primary page (TESTING_STRATEGY §6.9): WCAG 2.1 A/AA rules, zero violations of impact
 * "serious" or "critical". Runs on desktop and at 375 px (project mobile-375). Results are attached to the report.
 */
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

async function audit(page: Page, testInfo: TestInfo, label: string): Promise<void> {
  await page.waitForLoadState("networkidle");
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  await testInfo.attach(`axe-${label}.json`, { body: JSON.stringify(results.violations, null, 2), contentType: "application/json" });
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(
    blocking.map((v) => `${v.id} (${v.impact}): ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(", ")}`),
    `axe violations on ${label}`,
  ).toEqual([]);
}

test.describe("accessibility: public pages", () => {
  for (const route of ["/login", "/signup"]) {
    test(`axe ${route}`, async ({ page }, testInfo) => {
      const res = await page.goto(route);
      expect(res?.status() ?? 0).toBeLessThan(400);
      await audit(page, testInfo, route.slice(1));
    });
  }
});

test.describe("accessibility: authenticated pages", () => {
  test("axe on overview, datasets, Ask AI, AI policy and settings", async ({ page, user }, testInfo) => {
    const slug = await apiSignup(page.request, user); // page.request shares cookies with the page
    // Routes as merged in frontend/src/app/o/[org]/** (paths from frontend/src/lib/nav.ts)
    for (const [label, route] of [
      ["overview", `/o/${slug}/dashboard`],
      ["datasets", `/o/${slug}/data/datasets`],
      ["ask", `/o/${slug}/ask`],
      ["dashboards", `/o/${slug}/dashboards`],
      ["ai-policy", `/o/${slug}/settings/ai-policy`],
      ["settings", `/o/${slug}/settings`],
    ] as const) {
      const res = await page.goto(route);
      expect(res?.status() ?? 0, route).toBeLessThan(400);
      await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
      await audit(page, testInfo, label);
    }
  });

  test("dark and light themes keep contrast compliant", async ({ page, user }, testInfo) => {
    const slug = await apiSignup(page.request, user);
    await page.goto(`/o/${slug}/dashboard`);
    // Theme lives in the account menu (frontend/src/components/shell/user-menu.tsx): radio items System/Light/Dark
    for (const theme of ["Dark", "Light"] as const) {
      await page.getByRole("button", { name: /^Account menu for / }).click();
      const item = page.getByRole("menuitemradio", { name: theme });
      await item.click();
      await page.keyboard.press("Escape").catch(() => undefined);
      await audit(page, testInfo, `overview-${theme.toLowerCase()}`);
    }
  });
});
