import fs from "node:fs";
import path from "node:path";
import { chromium, expect } from "@playwright/test";

export const DEFAULT_BASE_URL = "http://127.0.0.1:18086";

export function ean13ForIndex(index) {
  const base = `885${String(index + 1).padStart(9, "0")}`.slice(0, 12);
  const check = (10 - ([...base].reduce((sum, digit, pos) => sum + (pos % 2 ? 3 : 1) * Number(digit), 0) % 10)) % 10;
  return `${base}${check}`;
}

export function itemCode(index) {
  return `KM-${String(index + 1).padStart(5, "0")}`;
}

export function median(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function percentile(values, p) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  return sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)];
}

function round(value) {
  return Number.isFinite(value) ? Number(value.toFixed(2)) : value;
}

export function readEnvFile(filePath) {
  const result = {};
  if (!filePath || !fs.existsSync(filePath)) return result;
  for (const line of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (match) result[match[1]] = match[2];
  }
  return result;
}

function findChromeExecutable() {
  const chromeRoot = "/opt/.devin/chrome/chrome";
  try {
    const versions = fs.readdirSync(chromeRoot).filter((entry) => entry.startsWith("linux-")).sort();
    for (let index = versions.length - 1; index >= 0; index -= 1) {
      const candidate = path.join(chromeRoot, versions[index], "chrome-linux64/chrome");
      if (fs.existsSync(candidate)) return candidate;
    }
  } catch {
    // Fall through to the PATH shim.
  }
  return process.env.CHROME_PATH || "google-chrome";
}

export async function launchBrowser({ headless = true } = {}) {
  return chromium.launch({
    headless,
    executablePath: findChromeExecutable(),
    args: [
      "--disable-dev-shm-usage",
      "--js-flags=--expose-gc",
      "--disable-features=TranslateUI",
    ],
  });
}

export async function loginContext(context, baseUrl, user, password) {
  const response = await context.request.post(`${baseUrl}/api/method/login`, {
    form: { usr: user, pwd: password },
    timeout: 15_000,
  });
  if (!response.ok()) {
    throw new Error(`Login failed with HTTP ${response.status()}`);
  }
}

export async function openCounter(page, baseUrl) {
  await page.goto(`${baseUrl}/khitmee_sales`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await expect(page.locator("#warehouse option").first()).toBeAttached({ timeout: 15_000 });
  await expect(page.locator("#status")).toContainText(/Ready|error/i, { timeout: 15_000 });

  const warehouseLabels = await page.locator("#warehouse option").allTextContents();
  const shopIndex = warehouseLabels.findIndex((label) => /shop 1/i.test(label));
  if (shopIndex >= 0) {
    const value = await page.locator("#warehouse option").nth(shopIndex).getAttribute("value");
    await page.selectOption("#warehouse", value);
  }

  const customerValues = await page.locator("#customer option").evaluateAll((options) =>
    options.map((node) => node.value).filter(Boolean),
  );
  if (customerValues.length) await page.selectOption("#customer", customerValues[0]);
  await page.locator("#item-search").focus();
}

function installCounterProbe() {
  if (window.__counterProbeInstalled) return;
  window.__counterProbeInstalled = true;
  const reset = () => ({
    marks: [],
    keydowns: [],
    mutationBatches: 0,
    mutationRecords: 0,
    addedNodes: 0,
    removedNodes: 0,
    firstMutationAt: null,
    lastMutationAt: null,
    longTasks: [],
  });
  window.__counterProbe = reset();
  window.__counterProbeReset = () => {
    window.__counterProbe = reset();
  };
  window.__counterProbeMark = (label) => {
    window.__counterProbe.marks.push({ label, time: performance.now() });
  };
  document.addEventListener("keydown", (event) => {
    const probe = window.__counterProbe;
    probe.keydowns.push({ key: event.key, time: performance.now() });
    if (probe.keydowns.length > 500) probe.keydowns.splice(0, probe.keydowns.length - 500);
  }, true);
  const observer = new MutationObserver((records) => {
    const probe = window.__counterProbe;
    const now = performance.now();
    probe.mutationBatches += 1;
    probe.mutationRecords += records.length;
    if (probe.firstMutationAt === null) probe.firstMutationAt = now;
    probe.lastMutationAt = now;
    for (const record of records) {
      probe.addedNodes += record.addedNodes.length;
      probe.removedNodes += record.removedNodes.length;
    }
  });
  observer.observe(document, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["hidden", "class", "aria-selected", "aria-activedescendant", "disabled"],
  });
  if ("PerformanceObserver" in window) {
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          window.__counterProbe.longTasks.push({ duration: entry.duration, startTime: entry.startTime });
        }
      }).observe({ type: "longtask", buffered: false });
    } catch {
      // longtask is unavailable in some Chromium configurations.
    }
  }
}

export async function installProbe(page) {
  await page.addInitScript(installCounterProbe);
  await page.evaluate(installCounterProbe);
}

export async function resetProbe(page) {
  await page.evaluate(() => window.__counterProbeReset && window.__counterProbeReset());
}

export async function probeSummary(page) {
  return page.evaluate(() => {
    const probe = window.__counterProbe || {};
    return {
      mutationBatches: probe.mutationBatches || 0,
      mutationRecords: probe.mutationRecords || 0,
      addedNodes: probe.addedNodes || 0,
      removedNodes: probe.removedNodes || 0,
      longTaskCount: (probe.longTasks || []).length,
      longTaskTotalMs: Number((probe.longTasks || []).reduce((sum, entry) => sum + entry.duration, 0).toFixed(2)),
      longestTaskMs: Number(Math.max(0, ...(probe.longTasks || []).map((entry) => entry.duration)).toFixed(2)),
    };
  });
}

export class NetworkTracker {
  constructor(cdp) {
    this.cdp = cdp;
    this.requests = new Map();
    this.finished = [];
    this.bound = false;
  }

  async start() {
    if (this.bound) return;
    this.bound = true;
    this.cdp.on("Network.requestWillBeSent", (event) => {
      this.requests.set(event.requestId, {
        url: event.request.url,
        method: event.request.method,
        resourceType: event.type || "other",
        startTime: event.timestamp,
        encodedBytes: 0,
      });
      if (this.requests.size > 5000) {
        const oldest = this.requests.keys().next().value;
        this.requests.delete(oldest);
      }
    });
    this.cdp.on("Network.responseReceived", (event) => {
      const request = this.requests.get(event.requestId);
      if (!request) return;
      request.status = event.response.status;
      request.mimeType = event.response.mimeType;
    });
    this.cdp.on("Network.loadingFinished", (event) => {
      const request = this.requests.get(event.requestId);
      if (!request) return;
      request.encodedBytes = event.encodedDataLength || 0;
      request.durationMs = Math.max(0, (event.timestamp - request.startTime) * 1000);
      this.finished.push(request);
      this.requests.delete(event.requestId);
    });
    this.cdp.on("Network.loadingFailed", (event) => {
      const request = this.requests.get(event.requestId);
      if (!request) return;
      request.failed = event.errorText || "failed";
      request.durationMs = Math.max(0, (event.timestamp - request.startTime) * 1000);
      this.finished.push(request);
      this.requests.delete(event.requestId);
    });
    await this.cdp.send("Network.enable");
  }

  reset() {
    this.requests.clear();
    this.finished = [];
  }

  summary() {
    const rows = this.finished;
    const apiRows = rows.filter((row) => row.url.includes("/api/"));
    const byEndpoint = new Map();
    for (const row of apiRows) {
      let key;
      try {
        const url = new URL(row.url);
        key = `${row.method} ${url.pathname}${url.pathname.includes("/api/method/") ? "?" + (url.searchParams.get("q") || "").slice(0, 18) : ""}`;
      } catch {
        key = row.url;
      }
      const bucket = byEndpoint.get(key) || { count: 0, encodedBytes: 0, durations: [] };
      bucket.count += 1;
      bucket.encodedBytes += row.encodedBytes || 0;
      bucket.durations.push(row.durationMs || 0);
      byEndpoint.set(key, bucket);
    }
    return {
      requestCount: rows.length,
      encodedBytes: rows.reduce((sum, row) => sum + (row.encodedBytes || 0), 0),
      apiRequestCount: apiRows.length,
      apiEncodedBytes: apiRows.reduce((sum, row) => sum + (row.encodedBytes || 0), 0),
      failedCount: rows.filter((row) => row.failed || (row.status && row.status >= 400)).length,
      endpoints: Object.fromEntries([...byEndpoint.entries()].sort().map(([key, value]) => [key, {
        count: value.count,
        encodedBytes: value.encodedBytes,
        medianMs: round(median(value.durations)),
        p95Ms: round(percentile(value.durations, 95)),
      }])),
    };
  }
}

export async function startChromeTrace(cdp, outputFile) {
  const events = [];
  const onData = (params) => {
    if (Array.isArray(params.events)) events.push(...params.events);
  };
  const complete = new Promise((resolve) => {
    cdp.once("Tracing.tracingComplete", resolve);
  });
  cdp.on("Tracing.dataCollected", onData);
  await cdp.send("Tracing.start", {
    categories: "devtools.timeline,v8,blink,blink.user_timing,loading,toplevel",
    transferMode: "ReportEvents",
  });
  return async function stopChromeTrace() {
    try {
      await cdp.send("Tracing.end");
      await Promise.race([complete, new Promise((resolve) => setTimeout(resolve, 5000))]);
    } finally {
      cdp.off("Tracing.dataCollected", onData);
    }
    fs.mkdirSync(path.dirname(outputFile), { recursive: true });
    fs.writeFileSync(outputFile, JSON.stringify({ traceEvents: events }));
  };
}

export async function takeHeapSnapshot(cdp, outputFile) {
  const chunks = [];
  const onChunk = (params) => chunks.push(params.chunk || "");
  cdp.on("HeapProfiler.addHeapSnapshotChunk", onChunk);
  try {
    await cdp.send("HeapProfiler.enable");
    await cdp.send("HeapProfiler.takeHeapSnapshot");
  } finally {
    cdp.off("HeapProfiler.addHeapSnapshotChunk", onChunk);
  }
  fs.mkdirSync(path.dirname(outputFile), { recursive: true });
  fs.writeFileSync(outputFile, chunks.join(""));
}

export async function performanceMetrics(cdp) {
  await cdp.send("Performance.enable");
  const response = await cdp.send("Performance.getMetrics");
  return Object.fromEntries(response.metrics.map((metric) => [metric.name, metric.value]));
}

export function metricDelta(before, after) {
  const keys = [
    "ScriptDuration",
    "LayoutDuration",
    "RecalcStyleDuration",
    "TaskDuration",
    "LayoutCount",
    "RecalcStyleCount",
    "Nodes",
    "JSEventListeners",
    "Documents",
    "Frames",
    "JSHeapUsedSize",
    "JSHeapTotalSize",
  ];
  return Object.fromEntries(keys.map((key) => {
    const delta = (after[key] || 0) - (before[key] || 0);
    const duration = key.endsWith("Duration");
    return [key, round(duration ? delta * 1000 : delta)];
  }));
}

export async function collectGarbage(page, cdp) {
  await cdp.send("HeapProfiler.enable").catch(() => {});
  await cdp.send("HeapProfiler.collectGarbage").catch(() => {});
  await page.evaluate(() => window.gc && window.gc()).catch(() => {});
}

export async function waitForRenderedCondition(page, type, argument = {}, timeout = 7000) {
  return page.evaluate(({ type: conditionType, argument: conditionArgument, timeout: conditionTimeout }) => {
    const start = performance.now();
    return new Promise((resolve) => {
      let settled = false;
      function condition() {
        if (conditionType === "rowCountGreaterThan") {
          return document.querySelectorAll("#lines-body tr").length > conditionArgument.count;
        }
        if (conditionType === "rowCountEquals") {
          return document.querySelectorAll("#lines-body tr").length === conditionArgument.count;
        }
        if (conditionType === "resultsVisible") {
          const results = document.querySelector("#search-results");
          return !!results && !results.hidden && results.children.length > 0;
        }
        if (conditionType === "activeDescendantAtLeast") {
          const input = document.querySelector("#item-search");
          const value = input ? input.getAttribute("aria-activedescendant") || "" : "";
          const index = Number(value.replace("search-option-", ""));
          return Number.isFinite(index) && index >= conditionArgument.index;
        }
        if (conditionType === "qtyInputValue") {
          const input = document.querySelector(`#lines-body tr[data-index="${conditionArgument.index}"] input[data-action="qty"]`);
          return !!input && input.value === String(conditionArgument.value);
        }
        return false;
      }
      function finish(kind) {
        if (settled) return;
        settled = true;
        requestAnimationFrame(() => requestAnimationFrame(() => {
          resolve({ elapsedMs: performance.now() - start, timedOut: kind === "timeout" });
        }));
      }
      function poll() {
        if (settled) return;
        if (condition()) finish("condition");
        else requestAnimationFrame(poll);
      }
      setTimeout(() => finish("timeout"), conditionTimeout);
      poll();
    });
  }, { type, argument, timeout });
}

export async function measureSearchRender(page, term) {
  const input = page.locator("#item-search");
  await input.fill("");
  if (term.length > 1) await input.pressSequentially(term.slice(0, -1), { delay: 8 });
  const measurement = waitForRenderedCondition(page, "resultsVisible");
  await input.pressSequentially(term.slice(-1), { delay: 8 });
  const timing = await measurement;
  await expect(page.locator("#search-results li").first()).toBeVisible({ timeout: 7000 });
  return timing.elapsedMs;
}

export async function measureBarcodeScan(page, term, previousRowCount) {
  const input = page.locator("#item-search");
  await expect(input).toHaveValue("");
  await input.pressSequentially(term, { delay: 3 });
  const measurement = waitForRenderedCondition(page, "rowCountGreaterThan", { count: previousRowCount });
  await page.keyboard.press("Enter");
  const timing = await measurement;
  if (timing.timedOut) throw new Error(`Timed out waiting for scan of ${term} to add a row`);
  await expect(page.locator("#lines-body tr")).toHaveCount(previousRowCount + 1);
  return timing.elapsedMs;
}

export async function measureBarcodeRepeat(page, term, rowIndex, expectedQty) {
  const input = page.locator("#item-search");
  await expect(input).toHaveValue("");
  await input.pressSequentially(term, { delay: 3 });
  const measurement = waitForRenderedCondition(page, "qtyInputValue", { index: rowIndex, value: String(expectedQty) });
  await page.keyboard.press("Enter");
  const timing = await measurement;
  if (timing.timedOut) throw new Error(`Timed out waiting for repeat scan of ${term}`);
  await expect(page.locator(`#lines-body tr[data-index="${rowIndex}"] input[data-action="qty"]`)).toHaveValue(String(expectedQty));
  return timing.elapsedMs;
}

export async function measureKeyboardNavigation(page, term) {
  const input = page.locator("#item-search");
  await input.fill(term);
  await expect(page.locator("#search-results li").first()).toBeVisible({ timeout: 7000 });
  const measurement = waitForRenderedCondition(page, "activeDescendantAtLeast", { index: 1 });
  await page.keyboard.press("ArrowDown");
  const timing = await measurement;
  if (timing.timedOut) throw new Error("Timed out waiting for autocomplete keyboard navigation");
  return timing.elapsedMs;
}

export async function measureQuantityIncrement(page) {
  const firstInput = page.locator('#lines-body tr[data-index="0"] input[data-action="qty"]');
  await expect(firstInput).toHaveValue("1");
  const measurement = waitForRenderedCondition(page, "qtyInputValue", { index: 0, value: "2" });
  await page.locator('#lines-body tr[data-index="0"] button[data-action="inc"]').click();
  const timing = await measurement;
  if (timing.timedOut) throw new Error("Timed out waiting for quantity increment rendering");
  return timing.elapsedMs;
}

export async function applyThrottling(cdp, profile) {
  if (profile.cpuRate && profile.cpuRate > 1) {
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: profile.cpuRate });
  }
  if (profile.network) {
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: profile.network.latencyMs,
      downloadThroughput: profile.network.downloadThroughput,
      uploadThroughput: profile.network.uploadThroughput,
    });
  }
}

export async function clearThrottling(cdp) {
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 1 }).catch(() => {});
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    latency: 0,
    downloadThroughput: -1,
    uploadThroughput: -1,
  }).catch(() => {});
}

export const PROFILES = {
  normal: {
    name: "normal",
    cpuRate: 1,
    network: null,
  },
  throttled: {
    name: "throttled-cpu4x-slow3g",
    cpuRate: 4,
    network: {
      latencyMs: 400,
      downloadThroughput: 50 * 1024,
      uploadThroughput: 50 * 1024,
    },
  },
};

export async function clearLines(page) {
  if (await page.locator("#lines-body tr").count()) {
    await page.locator("#clear-lines").click();
    await expect(page.locator("#lines-body tr")).toHaveCount(0);
  }
}
