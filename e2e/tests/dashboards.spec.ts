import {
  apiIngestCsv,
  apiSignup,
  makeUser,
  apiUnreadCount,
  askIsPlanned,
  askQuestion,
  csrfToken,
  expect,
  expectSqlAndRegions,
  itemsOf,
  REGIONS,
  SALES_CSV,
  test,
} from "./fixtures";

/**
 * W3 flows (CONTRACT "W3 additions"), driven through accessible roles and labels:
 *  1. Ask AI -> "Pin to dashboard" on the answer -> a NEW dashboard -> the dashboard page renders the widget, whose
 *     data the API re-executes (`GET /dashboards/{id}/widgets/{wid}/data`, never stored rows).
 *  2. Notifications bell in the header: its accessible name carries the unread count the API reports, the popover
 *     lists the notifications (or the empty state), and "Mark all read" clears the count in the API too.
 * Data is the synthetic sales.csv ingested through the public API. A step whose UI is not merged yet is recorded as a
 * `not-exercised` annotation, never faked.
 */

function notExercised(description: string): void {
  test.info().annotations.push({ type: "not-exercised", description });
}

test.describe("W3: dashboards and notifications", () => {
  test("an Ask AI answer can be pinned to a new dashboard and its widget renders live data", async ({ page, user }) => {
    const slug = await apiSignup(page.request, user); // page.request shares the page's cookies
    await apiIngestCsv(page.request, slug, SALES_CSV, "E2E Sales");

    await page.goto(`/o/${slug}/ask`);
    if (await askIsPlanned(page)) {
      notExercised("dashboard pin flow: /o/<slug>/ask is still the Planned placeholder");
      return;
    }
    await askQuestion(page, "total revenue by region");
    await expectSqlAndRegions(page);

    const pin = page.getByRole("button", { name: /pin to dashboard/i }).last();
    if (!(await pin.isVisible())) {
      notExercised("dashboard pin flow: the Ask AI answer has no 'Pin to dashboard' action yet");
      return;
    }
    await pin.click();
    const dialog = page.getByRole("dialog", { name: /pin to dashboard/i });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("combobox", { name: "Dashboard" }).click();
    await page.getByRole("option", { name: /new dashboard/i }).click();
    await dialog.getByLabel("New dashboard name").fill("E2E Pins");
    await dialog.getByLabel("Widget title").fill("Revenue by region (pinned)");
    // Show the widget as a table: its rows are then asserted cell by cell on the dashboard page (a chart's axis
    // labels may be thinned out by the chart library, so they are not a reliable oracle).
    await dialog.getByRole("combobox", { name: "Widget type" }).click();
    await page.getByRole("option", { name: /^Table/ }).click();
    await dialog.getByRole("button", { name: /^pin$/i }).click();
    // As built (frontend W3 PinDialog): the dialog stays open on a success screen with "Close" and
    // "Open dashboard" instead of closing itself.
    await expect(dialog.getByText("The answer is on your dashboard.")).toBeVisible({ timeout: 30_000 });
    const open = dialog.getByRole("link", { name: "Open dashboard" });
    await expect(open).toHaveAttribute("href", new RegExp(`^/o/${slug}/dashboards/[0-9a-f-]{36}$`));
    const href = (await open.getAttribute("href"))!;
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(dialog).toBeHidden();

    // The API agrees: one new dashboard with exactly one widget whose data is re-executed by the guard.
    const boards = itemsOf<{ id: string; title: string; widget_count: number }>(
      await (await page.request.get(`/api/v1/orgs/${slug}/dashboards`)).json(),
    );
    const board = boards.find((b) => b.title === "E2E Pins");
    expect(board, JSON.stringify(boards)).toBeDefined();
    expect(href.endsWith(`/dashboards/${board!.id}`), `${href} vs ${board!.id}`).toBe(true);
    expect(board!.widget_count).toBe(1);
    const detail = await (await page.request.get(`/api/v1/orgs/${slug}/dashboards/${board!.id}`)).json();
    const widget = detail.widgets[0];
    expect(widget.title).toBe("Revenue by region (pinned)");
    expect(widget.kind).toBe("table");
    expect(widget.saved_query_id, "pinning an answer creates a SavedQuery").toBeTruthy();
    const data = await page.request.get(`/api/v1/orgs/${slug}/dashboards/${board!.id}/widgets/${widget.id}/data`);
    expect(data.status()).toBe(200);
    const body = await data.json();
    expect(body.row_count).toBeGreaterThanOrEqual(REGIONS.length);
    expect(body.refreshed_at).toBeTruthy();

    // The dashboard page (reached from the list) renders the widget with its live rows.
    await page.goto(`/o/${slug}/dashboards`);
    await page.getByRole("link", { name: /E2E Pins/ }).first().click();
    await expect(page).toHaveURL(new RegExp(`/o/${slug}/dashboards/${board!.id}$`));
    const tile = page.getByRole("region", { name: "Revenue by region (pinned)" });
    await expect(tile).toBeVisible({ timeout: 30_000 });
    const table = tile.getByRole("table");
    await expect(table).toBeVisible({ timeout: 30_000 });
    for (const region of REGIONS) {
      await expect(table.getByRole("cell", { name: region, exact: true }).first()).toBeVisible();
    }
  });

  test("the notifications bell reflects the API unread count and mark-all-read clears it", async ({ page, user }) => {
    const slug = await apiSignup(page.request, user);
    if ((await apiUnreadCount(page.request, slug)) === null) {
      notExercised("notifications bell: /orgs/<slug>/notifications is not routed yet");
      return;
    }
    // Ingestion is a notification producer (CONTRACT "W3 additions / Notifications").
    await apiIngestCsv(page.request, slug, SALES_CSV, "E2E Sales");
    await page.goto(`/o/${slug}/dashboard`);

    const bell = page.getByRole("button", { name: /^Notifications/ });
    await expect(bell).toBeVisible({ timeout: 30_000 });
    // The bell polls every 30 s: wait until its accessible name matches what the API reports.
    const unread = (await apiUnreadCount(page.request, slug)) ?? 0;
    if (unread > 0) {
      await expect(bell).toHaveAccessibleName(new RegExp(`^Notifications, ${unread} unread$`), { timeout: 45_000 });
    } else {
      await expect(bell).toHaveAccessibleName("Notifications");
    }
    await bell.click();
    if (unread === 0) {
      await expect(page.getByText("You're all caught up")).toBeVisible();
      notExercised("notifications: no producer notified the ingestion, so mark-all-read was not exercised");
      return;
    }
    const list = page.getByRole("list", { name: "Notifications" });
    await expect(list).toBeVisible();
    await expect(list.getByRole("button", { name: /^Unread: / }).first()).toBeVisible();
    await page.getByRole("button", { name: "Mark all read" }).click();
    await expect.poll(async () => apiUnreadCount(page.request, slug), { timeout: 15_000 }).toBe(0);
    await expect(bell).toHaveAccessibleName("Notifications", { timeout: 15_000 });
    await expect(list.getByRole("button", { name: /^Unread: / })).toHaveCount(0);
  });

  test("another user's notifications and dashboards never show up", async ({ page, user, browser, baseURL }) => {
    const slugA = await apiSignup(page.request, user);
    const created = await page.request.post(`/api/v1/orgs/${slugA}/dashboards`, {
      headers: { "X-CSRFToken": await csrfToken(page.request) },
      data: { title: "Org A private board" },
    });
    if (created.status() === 404) {
      notExercised("dashboards isolation: /orgs/<slug>/dashboards is not routed yet");
      return;
    }
    expect(created.status(), await created.text()).toBe(201);
    const ctxB = await browser.newContext({ baseURL });
    try {
      const pageB = await ctxB.newPage();
      const slugB = await apiSignup(pageB.request, makeUser("e2eb"));
      expect((await pageB.request.get(`/api/v1/orgs/${slugA}/dashboards`)).status()).toBe(404);
      expect((await pageB.request.get(`/api/v1/orgs/${slugA}/notifications`)).status()).toBe(404);
      const own = await pageB.request.get(`/api/v1/orgs/${slugB}/dashboards`);
      expect(own.status()).toBe(200);
      expect(await own.text()).not.toContain("Org A private board");
      await pageB.goto(`/o/${slugA}/dashboards`);
      await expect(pageB.getByText("Organization not found")).toBeVisible();
    } finally {
      await ctxB.close();
    }
  });
});
