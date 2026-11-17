import { defineConfig, devices } from "@playwright/test";

/**
 * NEXORA E2E. Runs against the full stack behind Caddy (docker compose up -d + Next.js on :3000).
 *   E2E_BASE_URL   override the target (default http://localhost:8080)
 *   E2E_SKIP_IF_DOWN=1  skip every test (instead of failing) when the stack is not reachable
 */
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:8080";

export default defineConfig({
  testDir: "./tests",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 1,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  globalSetup: "./global-setup.ts",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    locale: "en-IN",
    timezoneId: "Asia/Kolkata",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    {
      name: "mobile-375",
      use: { ...devices["Pixel 7"], viewport: { width: 375, height: 812 } },
      testMatch: /accessibility\/.*\.spec\.ts/,
    },
  ],
});
