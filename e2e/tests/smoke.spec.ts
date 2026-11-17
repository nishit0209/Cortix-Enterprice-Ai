import { SALES_CSV, expect, test, uiSignup } from "./fixtures";

/**
 * Primary smoke flow (TESTING_STRATEGY §6, flows 1-2):
 *   sign up -> lands on the org dashboard -> upload testdata sales.csv -> preview -> load -> dataset detail.
 * Uses accessible roles/labels only (no CSS selectors), so it follows the UI spec rather than markup details.
 */
test.describe("P2/P3 smoke: signup -> dashboard -> upload -> dataset", () => {
  test("a new user can create a workspace and load a CSV dataset", async ({ page, user }) => {
    const slug = await uiSignup(page, user);

    // Dashboard / overview of the new org
    await expect(page).toHaveURL(new RegExp(`/o/${slug}(/|/overview|/dashboard)?$`));
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("navigation")).toContainText(/datasets/i);
    // The AI policy defaults to synthetic_only and the UI says so somewhere visible (status pill or notice)
    await expect(page.getByText(/synthetic[ _]only|synthetic data only/i).first()).toBeVisible();

    // Datasets -> upload
    await page.getByRole("navigation").getByRole("link", { name: /datasets/i }).click();
    await expect(page).toHaveURL(new RegExp(`/o/${slug}/datasets`));
    await page.getByRole("button", { name: /upload|new dataset|add data/i }).first().click();
    await page.locator('input[type="file"]').first().setInputFiles(SALES_CSV);

    // Parse preview: detected columns are shown before loading
    await expect(page.getByText("order_id").first()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("revenue").first()).toBeVisible();
    const name = page.getByLabel(/dataset name|^name/i);
    if (await name.count()) await name.first().fill("E2E Sales");
    // testdata is synthetic: classify it so AI features are usable under the default policy
    const syntheticRadio = page.getByRole("radio", { name: /synthetic/i });
    const classification = page.locator("select").filter({ has: page.locator("option", { hasText: /synthetic/i }) });
    if (await syntheticRadio.count()) {
      await syntheticRadio.first().check();
    } else if (await classification.count()) {
      const value = await classification.first().locator("option", { hasText: /synthetic/i }).first().getAttribute("value");
      if (value) await classification.first().selectOption(value);
    }
    await page.getByRole("button", { name: /load|create dataset|confirm|import/i }).first().click();

    // Dataset detail: URL, header and a terminal ingest status
    await page.waitForURL(new RegExp(`/o/${slug}/datasets/[0-9a-f-]{36}`), { timeout: 60_000 });
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/sales/i);
    await expect(page.getByText(/ready|succeeded|loaded/i).first()).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText(/5,?0[02]0/).first()).toBeVisible(); // 5,020 rows (5,000 after de-duplication)

    // Preview tab renders data from the file
    const previewTab = page.getByRole("tab", { name: /preview/i });
    if (await previewTab.count()) await previewTab.click();
    await expect(page.getByText(/ORD-\d{4}-\d{6}/).first()).toBeVisible();
  });

  test("another org's dataset URL is a 404 page, not data", async ({ page, user, request }) => {
    const slug = await uiSignup(page, user);
    await page.goto(`/o/${slug}-does-not-exist/datasets`);
    await expect(page.getByText(/not found|404|doesn.t exist/i).first()).toBeVisible();
    const res = await request.get(`/api/v1/orgs/${slug}-does-not-exist/jobs`);
    expect([401, 403, 404]).toContain(res.status());
  });
});
