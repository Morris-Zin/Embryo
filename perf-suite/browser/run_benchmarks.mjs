#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { pipeline } from "node:stream/promises";
import { createGzip } from "node:zlib";
import { expect } from "@playwright/test";
import {
  DEFAULT_BASE_URL,
  PROFILES,
  NetworkTracker,
  applyThrottling,
  clearLines,
  clearThrottling,
  collectGarbage,
  ean13ForIndex,
  installProbe,
  itemCode,
  launchBrowser,
  loginContext,
  measureBarcodeScan,
  measureBarcodeRepeat,
  measureKeyboardNavigation,
  measureQuantityIncrement,
  measureSearchRender,
  median,
  metricDelta,
  openCounter,
  percentile,
  performanceMetrics,
  probeSummary,
  readEnvFile,
  resetProbe,
  takeHeapSnapshot,
} from "./lib/counter_bench.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "../..");

function usage() {
  console.log(`Usage: node run_benchmarks.mjs [options]

Options:
  --label <name>          Artifact label (default: baseline)
  --url <url>             Benchmark stack URL (default: ${DEFAULT_BASE_URL})
  --profiles <list>       normal,throttled or one profile (default: normal,throttled)
  --scenarios <list>      search,scan,repeat-scan,keyboard,lines,memory,page-load (default: search,scan,repeat-scan,keyboard,lines,memory)
  --line-counts <list>    Invoice sizes for the lines scenario (default: 10,50,200)
  --repeat <n>            Repetitions for search/scan/keyboard scenarios (default: 1)
  --headed                Run Chrome headed
  --help                  Show this help

Authentication is read from BENCH_ADMIN_PASSWORD/ADMIN_PASSWORD or perf-suite/docker/.env.
Artifacts are written below perf-suite/results/browser/ and never print credentials.
`);
}

function parseList(value) {
  return String(value || "").split(",").map((entry) => entry.trim()).filter(Boolean);
}

async function gzipArtifact(filePath) {
  if (!filePath || !fs.existsSync(filePath)) return null;
  const gzipPath = `${filePath}.gz`;
  await pipeline(fs.createReadStream(filePath), createGzip({ level: 9 }), fs.createWriteStream(gzipPath));
  fs.unlinkSync(filePath);
  return path.basename(gzipPath);
}

function artifactName(label, profile, scenario, extension) {
  return `${label}-${profile.name}-${scenario}`.replace(/[^A-Za-z0-9_.-]+/g, "-") + extension;
}

async function createHarness(browser, options, profile) {
  const context = await browser.newContext({
    viewport: { width: 1360, height: 850 },
  });
  await loginContext(context, options.baseUrl, options.user, options.password);
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  const network = new NetworkTracker(cdp);
  await network.start();
  await applyThrottling(cdp, profile);
  await installProbe(page);
  await openCounter(page, options.baseUrl);
  return { browser, context, page, cdp, network };
}

async function runMeasuredScenario(harness, outputDir, label, profile, scenario, captureTrace, fn) {
  const { browser, page, cdp, network } = harness;
  network.reset();
  await resetProbe(page);
  await collectGarbage(page, cdp);
  const metricsBefore = await performanceMetrics(cdp);
  const traceFile = captureTrace ? path.join(outputDir, artifactName(label, profile, scenario, ".trace.json")) : null;
  if (traceFile) {
    await browser.startTracing(page, {
      path: traceFile,
      categories: ["devtools.timeline", "v8", "blink", "blink.user_timing", "loading"],
    });
  }
  const started = performance.now();
  let data;
  try {
    data = await fn();
    await page.waitForTimeout(120);
  } finally {
    if (traceFile) await browser.stopTracing();
  }
  const metricsAfter = await performanceMetrics(cdp);
  const compressedTrace = traceFile ? await gzipArtifact(traceFile) : null;
  const probe = await probeSummary(page);
  return {
    scenario,
    profile: profile.name,
    durationMs: Number((performance.now() - started).toFixed(2)),
    data,
    metricsBefore: Object.fromEntries(Object.entries(metricsBefore).filter(([, value]) => Number.isFinite(value))),
    metricsDelta: metricDelta(metricsBefore, metricsAfter),
    probe,
    network: network.summary(),
    artifacts: {
      trace: compressedTrace,
    },
  };
}

async function pageLoadScenario(harness) {
  const { page } = harness;
  await page.reload({ waitUntil: "domcontentloaded", timeout: 30_000 });
  await expect(page.locator("#item-search")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator("#status")).toContainText(/Ready/i, { timeout: 15_000 });
  return page.evaluate(() => {
    const navigation = performance.getEntriesByType("navigation")[0];
    return navigation ? {
      domContentLoadedMs: navigation.domContentLoadedEventEnd,
      loadMs: navigation.loadEventEnd,
      responseMs: navigation.responseEnd,
      transferBytes: navigation.transferSize,
      encodedBodyBytes: navigation.encodedBodySize,
    } : {};
  });
}

async function searchScenario(page) {
  const terms = ["KM-00001", "Mekong", "Beverage"];
  const latencies = [];
  for (const term of terms) latencies.push(await measureSearchRender(page, term));
  await page.keyboard.press("Escape");
  return {
    terms,
    latenciesMs: latencies.map((value) => Number(value.toFixed(2))),
    medianMs: Number(median(latencies).toFixed(2)),
    p95Ms: Number(percentile(latencies, 95).toFixed(2)),
  };
}

async function scanScenario(page, startIndex = 0, count = 3) {
  const latencies = [];
  let rows = await page.locator("#lines-body tr").count();
  for (let index = startIndex; index < startIndex + count; index += 1) {
    latencies.push(await measureBarcodeScan(page, ean13ForIndex(index), rows));
    rows += 1;
  }
  await clearLines(page);
  return {
    scanned: count,
    latenciesMs: latencies.map((value) => Number(value.toFixed(2))),
    medianMs: Number(median(latencies).toFixed(2)),
    p95Ms: Number(percentile(latencies, 95).toFixed(2)),
    maxMs: Number(Math.max(...latencies).toFixed(2)),
  };
}

async function repeatScanScenario(page) {
  await clearLines(page);
  const barcode = ean13ForIndex(25);
  await measureBarcodeScan(page, barcode, 0);
  const latencies = [];
  for (let qty = 2; qty <= 4; qty += 1) {
    latencies.push(await measureBarcodeRepeat(page, barcode, 0, qty));
  }
  return {
    latenciesMs: latencies.map((value) => Number(value.toFixed(2))),
    medianMs: Number(median(latencies).toFixed(2)),
    maxMs: Number(Math.max(...latencies).toFixed(2)),
  };
}

async function keyboardScenario(page) {
  const latencies = [];
  for (let iteration = 0; iteration < 2; iteration += 1) {
    latencies.push(await measureKeyboardNavigation(page, "KM-000"));
    await page.keyboard.press("Escape");
    await page.locator("#item-search").fill("");
  }
  return {
    latenciesMs: latencies.map((value) => Number(value.toFixed(2))),
    medianMs: Number(median(latencies).toFixed(2)),
    maxMs: Number(Math.max(...latencies).toFixed(2)),
  };
}

async function linesScenario(page, lineCount, itemOffset) {
  await clearLines(page);
  const scanLatencies = [];
  const started = performance.now();
  let rows = 0;
  for (let index = 0; index < lineCount; index += 1) {
    scanLatencies.push(await measureBarcodeScan(page, itemCode(itemOffset + index), rows));
    rows += 1;
  }
  const buildMs = performance.now() - started;
  await expect(page.locator("#lines-body tr")).toHaveCount(lineCount);
  const quantityIncrementMs = await measureQuantityIncrement(page);
  const rowNodeCount = await page.locator("#lines-body tr").count();
  const domNodeCount = await page.evaluate(() => document.getElementsByTagName("*").length);
  return {
    lineCount,
    buildMs: Number(buildMs.toFixed(2)),
    scanLatenciesMs: scanLatencies.map((value) => Number(value.toFixed(2))),
    scanMedianMs: Number(median(scanLatencies).toFixed(2)),
    scanP95Ms: Number(percentile(scanLatencies, 95).toFixed(2)),
    scanMaxMs: Number(Math.max(...scanLatencies).toFixed(2)),
    quantityIncrementMs: Number(quantityIncrementMs.toFixed(2)),
    rowNodeCount,
    domNodeCount,
    rowsPerSecond: Number((lineCount / (buildMs / 1000)).toFixed(2)),
  };
}

async function memoryScenario(page, cdp, outputDir, label, profile) {
  const heapBeforeFile = path.join(outputDir, artifactName(label, profile, "memory-before", ".heapsnapshot"));
  const heapAfterFile = path.join(outputDir, artifactName(label, profile, "memory-after", ".heapsnapshot"));
  await clearLines(page);
  await collectGarbage(page, cdp);
  const beforeMetrics = await performanceMetrics(cdp);
  await takeHeapSnapshot(cdp, heapBeforeFile);
  const heapSeries = [{ cycle: 0, jsHeapUsedSize: beforeMetrics.JSHeapUsedSize || 0 }];

  for (let cycle = 1; cycle <= 6; cycle += 1) {
    let rows = 0;
    for (let index = 0; index < 10; index += 1) {
      await measureBarcodeScan(page, itemCode((cycle - 1) * 10 + index), rows);
      rows += 1;
    }
    await clearLines(page);
    await collectGarbage(page, cdp);
    await page.waitForTimeout(100);
    const metrics = await performanceMetrics(cdp);
    heapSeries.push({ cycle, jsHeapUsedSize: metrics.JSHeapUsedSize || 0 });
  }

  await takeHeapSnapshot(cdp, heapAfterFile);
  const afterMetrics = await performanceMetrics(cdp);
  const growth = (afterMetrics.JSHeapUsedSize || 0) - (beforeMetrics.JSHeapUsedSize || 0);
  return {
    cycles: 6,
    itemsPerCycle: 10,
    heapSeries,
    heapGrowthBytes: Math.round(growth),
    heapGrowthRatio: beforeMetrics.JSHeapUsedSize ? Number((afterMetrics.JSHeapUsedSize / beforeMetrics.JSHeapUsedSize).toFixed(3)) : null,
    artifacts: {
      heapBefore: path.basename(heapBeforeFile),
      heapAfter: path.basename(heapAfterFile),
    },
  };
}

async function main() {
  const { values } = parseArgs({
    options: {
      label: { type: "string", default: "baseline" },
      url: { type: "string", default: process.env.BENCH_URL || DEFAULT_BASE_URL },
      profiles: { type: "string", default: "normal,throttled" },
      scenarios: { type: "string", default: "search,scan,repeat-scan,keyboard,lines,memory" },
      "line-counts": { type: "string", default: "10,50,200" },
      repeat: { type: "string", default: "1" },
      headed: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });
  if (values.help) {
    usage();
    return;
  }

  const envFile = readEnvFile(path.join(repoRoot, "perf-suite/docker/.env"));
  const options = {
    baseUrl: values.url,
    user: process.env.BENCH_ADMIN_USER || "Administrator",
    password: process.env.BENCH_ADMIN_PASSWORD || process.env.ADMIN_PASSWORD || envFile.ADMIN_PASSWORD,
  };
  if (!options.password) throw new Error("Admin password is unavailable; expected BENCH_ADMIN_PASSWORD or perf-suite/docker/.env");

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outputDir = path.join(repoRoot, "perf-suite/results/browser", `${timestamp}-${values.label}`);
  fs.mkdirSync(outputDir, { recursive: true });

  const requestedProfiles = parseList(values.profiles);
  const scenarios = parseList(values.scenarios);
  const lineCounts = parseList(values["line-counts"]).map((value) => Number(value)).filter((value) => value > 0);
  const repeat = Math.max(1, Number(values.repeat) || 1);
  const results = [];
  const browser = await launchBrowser({ headless: !values.headed });

  try {
    const browserVersion = browser.version();
    const outputFile = path.join(outputDir, "benchmark.json");
    const writeOutput = () => {
      fs.writeFileSync(outputFile, `${JSON.stringify({
        meta: {
          label: values.label,
          createdAt: new Date().toISOString(),
          baseUrl: options.baseUrl,
          browserVersion,
          repeat,
          profiles: requestedProfiles,
          scenarios,
          lineCounts,
          gitCommit: process.env.GIT_COMMIT || null,
        },
        results,
      }, null, 2)}\n`);
    };
    const recordScenario = async (promise) => {
      const result = await promise;
      results.push(result);
      writeOutput();
      return result;
    };
    for (const profileName of requestedProfiles) {
      const profile = PROFILES[profileName] || Object.values(PROFILES).find((entry) => entry.name === profileName);
      if (!profile) throw new Error(`Unknown profile ${profileName}`);
      for (let iteration = 1; iteration <= repeat; iteration += 1) {
        const harness = await createHarness(browser, options, profile);
        try {
          const iterationSuffix = repeat > 1 ? `-r${iteration}` : "";
          if (scenarios.includes("page-load")) {
            await recordScenario(runMeasuredScenario(harness, outputDir, values.label + iterationSuffix, profile, "page-load", true, () => pageLoadScenario(harness)));
          }
          if (scenarios.includes("search")) {
            await recordScenario(runMeasuredScenario(harness, outputDir, values.label + iterationSuffix, profile, "search", true, () => searchScenario(harness.page)));
          }
          if (scenarios.includes("scan")) {
            await recordScenario(runMeasuredScenario(harness, outputDir, values.label + iterationSuffix, profile, "scan", true, () => scanScenario(harness.page)));
          }
          if (scenarios.includes("repeat-scan")) {
            await recordScenario(runMeasuredScenario(harness, outputDir, values.label + iterationSuffix, profile, "repeat-scan", true, () => repeatScanScenario(harness.page)));
          }
          if (scenarios.includes("keyboard")) {
            await recordScenario(runMeasuredScenario(harness, outputDir, values.label + iterationSuffix, profile, "keyboard", false, () => keyboardScenario(harness.page)));
          }
          if (scenarios.includes("lines") && iteration === 1) {
            const counts = profile.name === "normal" ? lineCounts : lineCounts.filter((count) => count <= 10);
            let itemOffset = 0;
            for (const count of counts.length ? counts : [10]) {
              await recordScenario(runMeasuredScenario(harness, outputDir, values.label + iterationSuffix, profile, `lines-${count}`, count >= 200, () => linesScenario(harness.page, count, itemOffset)));
              itemOffset += count;
            }
          }
          if (scenarios.includes("memory") && iteration === 1) {
            await recordScenario(runMeasuredScenario(harness, outputDir, values.label + iterationSuffix, profile, "memory", false, () =>
              memoryScenario(harness.page, harness.cdp, outputDir, values.label + iterationSuffix, profile),
            ));
          }
        } finally {
          await clearThrottling(harness.cdp);
          await harness.context.close();
        }
      }
    }

    const output = {
      meta: {
        label: values.label,
        createdAt: new Date().toISOString(),
        baseUrl: options.baseUrl,
        browserVersion,
        repeat,
        profiles: requestedProfiles,
        scenarios,
        lineCounts,
        gitCommit: process.env.GIT_COMMIT || null,
      },
      results,
    };
    fs.writeFileSync(outputFile, `${JSON.stringify(output, null, 2)}\n`);
    console.log(JSON.stringify({
      outputDir,
      benchmark: outputFile,
      scenarios: results.map((result) => ({
        scenario: result.scenario,
        profile: result.profile,
        durationMs: result.durationMs,
        key: result.data.medianMs ?? result.data.scanMedianMs ?? result.data.buildMs ?? result.data.heapGrowthBytes ?? result.data.loadMs ?? null,
        apiRequests: result.network.apiRequestCount,
        apiBytes: result.network.apiEncodedBytes,
        scriptDeltaMs: result.metricsDelta.ScriptDuration,
        layoutDeltaMs: result.metricsDelta.LayoutDuration,
        nodesDelta: result.metricsDelta.Nodes,
        mutationRecords: result.probe.mutationRecords,
        longTaskTotalMs: result.probe.longTaskTotalMs,
      })),
    }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
