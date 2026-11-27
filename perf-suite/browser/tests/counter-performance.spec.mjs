import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import {
  DEFAULT_BASE_URL,
  ean13ForIndex,
  installProbe,
  itemCode,
  loginContext,
  measureBarcodeScan,
  openCounter,
  probeSummary,
  readEnvFile,
  resetProbe,
} from "../lib/counter_bench.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "../../..");
const envFile = readEnvFile(path.join(repoRoot, "perf-suite/docker/.env"));
const baseUrl = process.env.BENCH_URL || DEFAULT_BASE_URL;
const password = process.env.BENCH_ADMIN_PASSWORD || process.env.ADMIN_PASSWORD || envFile.ADMIN_PASSWORD;

async function openAuthenticatedCounter(page, counterPath = "/khitmee_sales") {
  test.skip(!password, "BENCH_ADMIN_PASSWORD or perf-suite/docker/.env is required");
  await loginContext(page.context(), baseUrl, process.env.BENCH_ADMIN_USER || "Administrator", password);
  await openCounter(page, baseUrl, counterPath);
  await installProbe(page);
}

test.describe("cashier counter performance regressions", () => {
  test("counter static assets carry file-version cache keys", async ({ page }) => {
    await openAuthenticatedCounter(page);
    const scriptVersion = await page.locator('script[src*="counter_core.js"]').getAttribute("src");
    const styleVersion = await page.locator('link[href*="counter.css"]').getAttribute("href");
    for (const assetUrl of [scriptVersion, styleVersion]) {
      expect(assetUrl, "counter asset URL should be present").toBeTruthy();
      expect(new URL(assetUrl, baseUrl).searchParams.get("v"), "counter assets must bust browser cache when files change").toMatch(/^\d+$/);
    }
  });

  test("exact barcode scan uses one bounded search request and adds the correct row", async ({ page }) => {
    await openAuthenticatedCounter(page);
    const apiRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/method/khitmee_ops.api.search.search_items")) apiRequests.push(request.url());
    });

    await measureBarcodeScan(page, ean13ForIndex(0), 0);

    await expect(page.locator("#lines-body tr")).toHaveCount(1);
    await expect(page.locator('#lines-body tr[data-index="0"] strong')).toHaveText(itemCode(0));
    expect(apiRequests, "exactly one item-search API call per scanner Enter").toHaveLength(1);
    const response = await page.evaluate(async (url) => {
      const result = await fetch(url, { credentials: "same-origin" });
      return result.json();
    }, apiRequests[0]);
    expect(response.message.items[0]).not.toHaveProperty("cost_rate");
    expect(response.message.items[0]).not.toHaveProperty("valuation_rate");
    expect(response.message.items[0]).not.toHaveProperty("supplier");
  });

  test("re-scanning the same barcode reuses a bounded fresh client cache entry", async ({ page }) => {
    await openAuthenticatedCounter(page);
    const apiRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/method/khitmee_ops.api.search.search_items")) apiRequests.push(request.url());
    });
    const barcode = ean13ForIndex(0);
    const input = page.locator("#item-search");

    await input.pressSequentially(barcode, { delay: 3 });
    await page.keyboard.press("Enter");
    await expect(page.locator("#lines-body tr")).toHaveCount(1);
    await input.pressSequentially(barcode, { delay: 3 });
    await page.keyboard.press("Enter");

    await expect(page.locator("#lines-body tr")).toHaveCount(1);
    await expect(page.locator('#lines-body tr[data-index="0"] input[data-action="qty"]')).toHaveValue("2");
    expect(apiRequests, "an immediate repeat scan of the same barcode should not repeat the item-search API call").toHaveLength(1);
  });


  test("transfer item search uses explicit no-rate mode and adds a row without a price list", async ({ page }) => {
    await openAuthenticatedCounter(page, "/khitmee_transfer");
    const sourceValue = await page.locator("#warehouse").inputValue();
    const targetValues = await page.locator("#target-warehouse option").evaluateAll((options) =>
      options.map((node) => node.value).filter(Boolean),
    );
    const targetValue = targetValues.find((value) => value !== sourceValue);
    expect(targetValue, "seed data must provide a distinct target warehouse").toBeTruthy();
    await page.selectOption("#target-warehouse", targetValue);

    const apiRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/method/khitmee_ops.api.search.search_items")) apiRequests.push(request.url());
    });
    await page.locator("#item-search").fill("KM-000");
    await expect(page.locator("#search-results li").first()).toBeVisible();
    await expect(page.locator("#search-results li .rate").first()).toHaveText("—");
    await page.keyboard.press("Escape");
    await page.locator("#item-search").fill("");
    apiRequests.length = 0;

    await measureBarcodeScan(page, itemCode(0), 0);

    await expect(page.locator("#lines-body tr")).toHaveCount(1);
    expect(apiRequests).toHaveLength(1);
    expect(apiRequests[0]).toContain("include_rates=0");
    expect(apiRequests[0]).not.toContain("price_list=");
    await expect(page.locator('#lines-body tr[data-index="0"] strong')).toHaveText(itemCode(0));
  });

  test("quantity changes update one row without rebuilding unaffected invoice rows", async ({ page }) => {
    await openAuthenticatedCounter(page);
    await measureBarcodeScan(page, itemCode(0), 0);
    await measureBarcodeScan(page, itemCode(1), 1);

    const rowIdentity = await page.evaluate(() => {
      window.__counterRowsBefore = [...document.querySelectorAll("#lines-body tr")];
      return window.__counterRowsBefore.length;
    });
    expect(rowIdentity).toBe(2);

    await page.locator('#lines-body tr[data-index="0"] button[data-action="inc"]').click();
    await expect(page.locator('#lines-body tr[data-index="0"] input[data-action="qty"]')).toHaveValue("2");

    const preservedRows = await page.evaluate(() =>
      [...document.querySelectorAll("#lines-body tr")].filter((row, index) => row === window.__counterRowsBefore[index]).length,
    );
    expect(preservedRows, "incrementing quantity must not discard and recreate every row DOM node").toBe(2);
    await expect(page.locator("#total-qty")).toHaveText("3");
  });

  test("scanning ten distinct lines has a linear DOM-mutation budget", async ({ page }) => {
    await openAuthenticatedCounter(page);
    await resetProbe(page);
    let rows = 0;
    for (let index = 0; index < 10; index += 1) {
      await measureBarcodeScan(page, itemCode(index), rows);
      rows += 1;
    }
    const probe = await probeSummary(page);
    expect(probe.addedNodes, "ten scanned rows should append rows incrementally rather than rebuild the table").toBeLessThanOrEqual(180);
    expect(probe.mutationRecords, "ten scanned rows should stay within the incremental rendering mutation budget").toBeLessThanOrEqual(140);
  });

  test("autocomplete keyboard navigation preserves option DOM nodes", async ({ page }) => {
    await openAuthenticatedCounter(page);
    await page.locator("#item-search").fill("KM-000");
    await expect(page.locator("#search-results li").nth(1)).toBeVisible();
    await page.evaluate(() => {
      window.__searchOptionsBefore = [...document.querySelectorAll("#search-results li")];
    });
    await page.keyboard.press("ArrowDown");
    await expect(page.locator("#item-search")).toHaveAttribute("aria-activedescendant", "search-option-1");
    const preservedOptions = await page.evaluate(() =>
      [...document.querySelectorAll("#search-results li")].filter((row, index) => row === window.__searchOptionsBefore[index]).length,
    );
    expect(preservedOptions, "arrow-key navigation should update selection state without rebuilding the autocomplete list").toBeGreaterThanOrEqual(2);
  });

  test("repeated scan-and-clear cycles do not retain invoice row memory", async ({ page }) => {
    await openAuthenticatedCounter(page);
    const cdp = await page.context().newCDPSession(page);
    const collect = async () => {
      await cdp.send("HeapProfiler.enable");
      await cdp.send("HeapProfiler.collectGarbage");
      await page.evaluate(() => window.gc && window.gc());
      await page.waitForTimeout(100);
      const metrics = await cdp.send("Performance.getMetrics");
      return metrics.metrics.find((metric) => metric.name === "JSHeapUsedSize")?.value || 0;
    };
    await cdp.send("Performance.enable");
    const before = await collect();

    for (let cycle = 0; cycle < 3; cycle += 1) {
      let rows = 0;
      for (let index = 0; index < 5; index += 1) {
        await measureBarcodeScan(page, itemCode(cycle * 5 + index), rows);
        rows += 1;
      }
      await page.locator("#clear-lines").click();
      await expect(page.locator("#lines-body tr")).toHaveCount(0);
    }

    const after = await collect();
    expect(after - before, "heap growth after GC across repeated scan/clear cycles").toBeLessThan(4 * 1024 * 1024);
  });
});
