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
  test("axe on overview, datasets and settings", async ({ page, user }, testInfo) => {
    const slug = await apiSignup(page.request, user); // page.request shares cookies with the page
    for (const [label, route] of [
      ["overview", `/o/${slug}`],
      ["datasets", `/o/${slug}/datasets`],
      ["settings", `/o/${slug}/settings`],
    ] as const) {
      const res = await page.goto(route);
      expect(res?.status() ?? 0, route).toBeLessThan(400);
      await audit(page, testInfo, label);
    }
  });

  test("theme toggle keeps contrast compliant", async ({ page, user }, testInfo) => {
    const slug = await apiSignup(page.request, user);
    await page.goto(`/o/${slug}`);
    const toggle = page.getByRole("button", { name: /theme|dark mode|light mode/i });
    test.skip((await toggle.count()) === 0, "no theme toggle on this page");
    await toggle.first().click();
    await audit(page, testInfo, "overview-toggled-theme");
  });
});
