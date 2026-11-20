import { defineConfig, devices } from "@playwright/test";

/**
 * NEXORA E2E. Local run (CONTRACT "Local runtime": PostgreSQL only, no Docker):
 *   backend   `manage.py runserver` (or uvicorn) on :8000, after `manage.py seed_demo` if you want demo content
 *   frontend  `npm run dev` on :3000 (Next.js proxies /api and /healthz to the backend, frontend/next.config.ts)
 * Environment:
 *   E2E_BASE_URL        target origin (default http://localhost:3000; use http://localhost:8080 behind Caddy)
 *   E2E_SKIP_IF_DOWN=1  skip every test (instead of failing) when GET <base>/healthz is not reachable
 */
export const DEFAULT_BASE_URL = "http://localhost:3000";
const baseURL = process.env.E2E_BASE_URL || DEFAULT_BASE_URL;

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
