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

  test("browser catalog mode serves repeat barcode lookup locally after synchronization", async ({ page }) => {
    await openAuthenticatedCounter(page, "/khitmee_sales?khitmee_catalog=browser");
    const warehouse = await page.locator("#warehouse").inputValue();
    await page.waitForFunction((expectedWarehouse) => {
      const status = window.__khitmeeCatalogStatus;
      return status && status.state === "ready" && status.scope && status.scope.split("\u001f")[0] === expectedWarehouse;
    }, warehouse, { timeout: 30000 });
    const synced = await page.evaluate(() => window.__khitmeeCatalogStatus.itemCount);
    expect(synced, "browser catalog sync must load benchmark items").toBeGreaterThan(0);

    const searchApiRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/method/khitmee_ops.api.search.search_items")) searchApiRequests.push(request.url());
    });
    await measureBarcodeScan(page, ean13ForIndex(0), 0);

    await expect(page.locator("#lines-body tr")).toHaveCount(1);
    await expect(page.locator('#lines-body tr[data-index="0"] strong')).toHaveText(itemCode(0));
    expect(searchApiRequests, "synchronized browser catalog scans must not call server item search").toHaveLength(0);

    const fallbackRequest = page.waitForRequest((request) => request.url().includes("/api/method/khitmee_ops.api.search.search_items"), { timeout: 10000 });
    await page.locator("#item-search").fill("0000000000000");
    await page.keyboard.press("Enter");
    await fallbackRequest;
    expect(searchApiRequests.length, "an unknown barcode must fall back to authoritative server search").toBeGreaterThan(0);
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

  test("enter during an in-flight autocomplete search reuses the same request", async ({ page }) => {
    await openAuthenticatedCounter(page);
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: 400,
      downloadThroughput: 50 * 1024,
      uploadThroughput: 50 * 1024,
    });
    const barcode = ean13ForIndex(2);
    const apiRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/method/khitmee_ops.api.search.search_items") && request.url().includes(barcode)) {
        apiRequests.push(request.url());
      }
    });

    const firstRequest = page.waitForRequest((request) =>
      request.url().includes("/api/method/khitmee_ops.api.search.search_items") && request.url().includes(barcode),
      { timeout: 7000 },
    );
    await page.locator("#item-search").pressSequentially(barcode, { delay: 3 });
    await firstRequest;
    await page.keyboard.press("Enter");

    await expect(page.locator("#lines-body tr")).toHaveCount(1, { timeout: 7000 });
    expect(apiRequests, "Enter must join the in-flight search instead of duplicating it").toHaveLength(1);
  });


  test("visible suggestions refresh after the freshness window and preserve the selected item", async ({ page }) => {
    await openAuthenticatedCounter(page);
    let rate = 10;
    const requests = [];
    const item = (code) => ({
      item_code: code,
      item_name: code + " name",
      stock_uom: "Nos",
      rate,
      actual_qty: 8,
    });
    await page.route("**/api/method/khitmee_ops.api.search.search_items*", async (route) => {
      requests.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ message: { items: [item("TEST-A"), item("TEST-B")] } }),
      });
    });

    await page.locator("#item-search").fill("A");
    await expect(page.locator("#search-results li").nth(1)).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await expect(page.locator("#item-search")).toHaveAttribute("aria-activedescendant", "search-option-1");

    rate = 20;
    await page.evaluate(() => {
      const realNow = Date.now.bind(Date);
      Date.now = () => realNow() + 30001;
    });
    await page.keyboard.press("Enter");

    await expect(page.locator('#lines-body tr[data-index="0"] strong')).toHaveText("TEST-B");
    await expect(page.locator('#lines-body tr[data-index="0"] .line-rate')).toHaveText("20.00");
    expect(requests, "expired visible suggestions must be refreshed before Enter uses them").toHaveLength(2);
  });

  test("refresh omits the highlighted item without silently substituting another match", async ({ page }) => {
    await openAuthenticatedCounter(page);
    let includeSelected = true;
    const requests = [];
    const item = (code) => ({ item_code: code, item_name: code + " name", stock_uom: "Nos", rate: 10, actual_qty: 8 });
    await page.route("**/api/method/khitmee_ops.api.search.search_items*", async (route) => {
      requests.push(route.request().url());
      const items = includeSelected ? [item("TEST-A"), item("TEST-B")] : [item("TEST-A")];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ message: { items } }) });
    });

    await page.locator("#item-search").fill("A");
    await expect(page.locator("#search-results li").nth(1)).toBeVisible();
    await page.keyboard.press("ArrowDown");
    includeSelected = false;
    await page.evaluate(() => {
      const realNow = Date.now.bind(Date);
      Date.now = () => realNow() + 30001;
    });
    await page.keyboard.press("Enter");

    await expect.poll(() => requests.length, { message: "expired visible suggestions must be refreshed" }).toBe(2);
    await page.waitForTimeout(100);
    await expect(page.locator("#lines-body tr"), "a missing selected item must not be replaced by the first remaining match").toHaveCount(0);
    await expect(page.locator("#status")).toContainText(/no longer available/i);
  });

  for (const releaseOrder of ["oldest-first", "newest-first"]) {
    test("A-B-A pending search interleaving does not reuse an obsolete generation (" + releaseOrder + ")", async ({ page }) => {
      await openAuthenticatedCounter(page);
      const pending = [];
      await page.route("**/api/method/khitmee_ops.api.search.search_items*", (route) => {
        pending.push({ route, q: new URL(route.request().url()).searchParams.get("q") });
      });
      const fulfill = async (entry) => {
        await entry.route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            message: {
              items: [{ item_code: "TEST-" + entry.q, item_name: entry.q + " name", stock_uom: "Nos", rate: 10, actual_qty: 8 }],
            },
          }),
        });
      };

      await page.locator("#item-search").fill("A");
      await expect.poll(() => pending.filter((entry) => entry.q === "A").length).toBe(1);
      await page.locator("#item-search").fill("B");
      await expect.poll(() => pending.filter((entry) => entry.q === "B").length).toBe(1);
      await page.locator("#item-search").fill("A");
      await page.keyboard.press("Enter");
      await expect.poll(() => pending.filter((entry) => entry.q === "A").length, {
        message: "returning to A after B invalidated its generation must start a fresh lookup",
      }).toBe(2);

      const aRequests = pending.filter((entry) => entry.q === "A");
      const bRequests = pending.filter((entry) => entry.q === "B");
      const newestFirst = [aRequests[1], bRequests[0], aRequests[0]];
      const oldestFirst = [aRequests[0], bRequests[0], aRequests[1]];
      for (const entry of releaseOrder === "newest-first" ? newestFirst : oldestFirst) await fulfill(entry);

      await expect(page.locator("#lines-body tr")).toHaveCount(1);
      await expect(page.locator('#lines-body tr[data-index="0"] strong')).toHaveText("TEST-A");
    });
  }

  test("failed scan searches never add stale autocomplete items", async ({ page }) => {
    await openAuthenticatedCounter(page);
    await page.locator("#item-search").fill(itemCode(0));
    await expect(page.locator("#search-results li").first()).toBeVisible();
    await page.route("**/api/method/khitmee_ops.api.search.search_items*", (route) => route.abort());

    await page.locator("#item-search").fill(itemCode(1));
    await page.keyboard.press("Enter");
    await expect(page.locator("#status")).toContainText(/error|failed|fetch/i);
    await page.waitForTimeout(200);

    await expect(page.locator("#lines-body tr")).toHaveCount(0);
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
    expect(probe.addedNodes, "ten scanned rows should append rows incrementally rather than rebuild the table").toBeLessThanOrEqual(70);
    expect(probe.mutationRecords, "ten scanned rows should stay within the incremental rendering mutation budget").toBeLessThanOrEqual(70);
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
