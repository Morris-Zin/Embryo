import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 90_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"], ["json", { outputFile: "../results/browser/playwright-results.json" }]],
  use: {
    headless: true,
    viewport: { width: 1360, height: 850 },
    launchOptions: {
      executablePath: process.env.CHROME_PATH || "/home/ubuntu/.local/bin/google-chrome",
      args: ["--disable-dev-shm-usage", "--js-flags=--expose-gc"],
    },
  },
});
