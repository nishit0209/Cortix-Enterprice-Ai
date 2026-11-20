import { apiSignup, expect, makeUser, SALES_CSV, test, uiSignup } from "./fixtures";

/**
 * Primary smoke flow (TESTING_STRATEGY §6, flows 1-2), aligned with the merged frontend (frontend/src on main):
 *   /signup (SignupForm) -> /o/<slug>/dashboard -> sidebar "Datasets" -> /o/<slug>/data/datasets
 *   -> "Upload dataset" dialog (Dropzone + Dataset name + Classification select + attestation)
 *   -> "Upload and process" -> live job stream reaches "Ready" -> "Open dataset" -> dataset detail
 *   -> sidebar "Ask AI" -> ask "total revenue by region" (or record that the module is still a Planned placeholder).
 * Selectors are accessible roles/labels (plus the Dropzone's documented data-testid for the hidden file input).
 */
const REGIONS = ["North", "South", "East", "West", "Central"];

test.describe("P2/P3 smoke: signup -> dashboard -> upload -> dataset -> Ask AI", () => {
  test("a new user can create a workspace, load a CSV dataset and reach Ask AI", async ({ page, user }) => {
    const slug = await uiSignup(page, user);

    // Overview dashboard of the new org
    await expect(page).toHaveURL(new RegExp(`/o/${slug}/dashboard$`));
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav.getByRole("link", { name: "Datasets" })).toBeVisible();
    // AI policy defaults to synthetic_only and the overview says so (POLICY_LABELS.synthetic_only = "Synthetic only")
    await expect(page.getByText(/synthetic[ _]only/i).first()).toBeVisible();

    // Datasets -> upload dialog
    await nav.getByRole("link", { name: "Datasets" }).click();
    await expect(page).toHaveURL(new RegExp(`/o/${slug}/data/datasets$`));
    await expect(page.getByRole("heading", { level: 1, name: "Datasets" })).toBeVisible();
    await page.getByRole("button", { name: "Upload dataset", exact: true }).click();

    const dialog = page.getByRole("dialog", { name: "Upload dataset" });
    await expect(dialog).toBeVisible();
    await dialog.getByTestId("dropzone-input").setInputFiles(SALES_CSV);
    await expect(dialog.getByText("sales.csv")).toBeVisible();
    await dialog.getByLabel("Dataset name").fill("E2E Sales");

    // testdata is synthetic: classify it (with attestation) so AI features are usable under the default policy.
    // Anchored regex: the "Internal" option's description also mentions synthetic_only.
    const classification = dialog.getByRole("combobox", { name: "Classification" });
    await classification.click();
    await page.getByRole("option", { name: /^Synthetic/ }).click();
    await expect(classification).toContainText("Synthetic");
    const attest = dialog.getByRole("checkbox");
    await attest.click();
    await expect(attest).toBeChecked();

    await dialog.getByRole("button", { name: "Upload and process" }).click();
    // Live job stream: Ingest -> Profile -> Quality -> Warehouse, then the dialog reports completion.
    await expect(dialog.getByText("Ingested, profiled and quality-scored.")).toBeVisible({ timeout: 180_000 });
    await dialog.getByRole("link", { name: "Open dataset" }).click();

    // Dataset detail: URL, header, terminal status, row count
    await page.waitForURL(new RegExp(`/o/${slug}/data/datasets/[0-9a-f-]{36}$`), { timeout: 60_000 });
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("E2E Sales");
    await expect(page.getByText(/^ready$/i).first()).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText(/^5,0[02]0$/).first()).toBeVisible(); // 5,020 rows (5,000 after de-duplication)

    // Preview tab (default) renders rows from the file; Schema tab lists profiled columns
    await expect(page.getByRole("tab", { name: /preview/i })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText(/ORD-\d{4}-\d{6}/).first()).toBeVisible({ timeout: 60_000 });
    await page.getByRole("tab", { name: /schema/i }).click();
    await expect(page.getByText("revenue").first()).toBeVisible();

    // Ask AI
    await nav.getByRole("link", { name: "Ask AI" }).click();
    await expect(page).toHaveURL(new RegExp(`/o/${slug}/ask$`));
    await expect(page.getByRole("heading", { level: 1, name: "Ask AI" })).toBeVisible();
    if (await page.getByText("Ask AI is on the roadmap").isVisible()) {
      // The merged frontend still renders the honest PlannedModule placeholder (nav status "planned", Wave 3).
      // Nothing is simulated there, so there is no question box to drive yet: record it instead of faking a pass.
      test.info().annotations.push({
        type: "not-exercised",
        description: "Ask AI question step: /o/<slug>/ask is still the Planned placeholder in frontend/src on main",
      });
      return;
    }
    const box = page.getByRole("textbox", { name: /ask|question|message/i }).first();
    await box.fill("total revenue by region");
    await box.press("Enter");
    // Deterministic builder path: the answer shows its SQL and the 5 regions of sales.csv.
    await expect(page.getByText(/\bselect\b/i).first()).toBeVisible({ timeout: 90_000 });
    for (const region of REGIONS) {
      await expect(page.getByText(region, { exact: true }).first()).toBeVisible();
    }
  });

  test("another org's workspace is 'not found' in the UI, 404 for non-members and 401 anonymously", async ({
    page,
    user,
    browser,
    baseURL,
  }) => {
    const slugA = await uiSignup(page, user);

    // A second, unrelated user in a fresh browser context (separate cookies)
    const ctxB = await browser.newContext({ baseURL });
    try {
      const pageB = await ctxB.newPage();
      const slugB = await apiSignup(pageB.request, makeUser("e2eb"));
      expect(slugB).not.toBe(slugA);

      await pageB.goto(`/o/${slugA}/data/datasets`);
      await expect(pageB.getByText("Organization not found")).toBeVisible();
      await expect(pageB.getByRole("link", { name: /^open /i })).toHaveAttribute("href", `/o/${slugB}/dashboard`);

      // Authenticated non-member -> 404 (no existence oracle)
      const asNonMember = await pageB.request.get(`/api/v1/orgs/${slugA}/datasets`);
      expect(asNonMember.status()).toBe(404);
    } finally {
      await ctxB.close();
    }

    // Anonymous -> 401 (as-built: unauthenticated tenant requests are 401, not 404)
    const anon = await browser.newContext({ baseURL });
    try {
      const res = await anon.request.get(`/api/v1/orgs/${slugA}/datasets`);
      expect(res.status()).toBe(401);
    } finally {
      await anon.close();
    }

    // Unknown dataset id inside the member's own org -> "Dataset not found" state, not data
    await page.goto(`/o/${slugA}/data/datasets/00000000-0000-4000-8000-000000000000`);
    await expect(page.getByText(/dataset not found/i)).toBeVisible();
  });
});
