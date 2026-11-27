#!/usr/bin/env node
import fs from "node:fs";

function readBenchmark(path) {
  const data = JSON.parse(fs.readFileSync(path, "utf8"));
  const key = new Map();
  for (const result of data.results) {
    const id = `${result.profile}:${result.scenario}`;
    if (!key.has(id)) key.set(id, []);
    key.get(id).push(result);
  }
  for (const group of key.values()) {
    group.sort((a, b) => a.durationMs - b.durationMs);
  }
  return key;
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function metric(result) {
  return {
    durationMs: result.durationMs,
    keyMs: result.data.medianMs ?? result.data.scanMedianMs ?? result.data.quantityIncrementMs ?? result.data.loadMs ?? null,
    apiRequests: result.network.apiRequestCount,
    apiBytes: result.network.apiEncodedBytes,
    mutationRecords: result.probe.mutationRecords,
    addedNodes: result.probe.addedNodes,
    removedNodes: result.probe.removedNodes,
    layoutCount: result.metricsDelta.LayoutCount,
    styleCount: result.metricsDelta.RecalcStyleCount,
    longTaskMs: result.probe.longTaskTotalMs,
    heapGrowthBytes: result.data.heapGrowthBytes ?? null,
  };
}

const [beforePath, afterPath] = process.argv.slice(2);
if (!beforePath || !afterPath) {
  console.error("usage: node compare_benchmarks.mjs <before/benchmark.json> <after/benchmark.json>");
  process.exit(2);
}
const before = readBenchmark(beforePath);
const after = readBenchmark(afterPath);
const rows = [];
for (const [id, beforeGroup] of before) {
  const afterGroup = after.get(id);
  if (!afterGroup) continue;
  const b = metric(beforeGroup[Math.floor(beforeGroup.length / 2)]);
  const a = metric(afterGroup[Math.floor(afterGroup.length / 2)]);
  const row = { scenario: id, before: b, after: a };
  for (const name of ["durationMs", "keyMs", "mutationRecords", "addedNodes", "layoutCount", "longTaskMs", "apiRequests", "apiBytes", "heapGrowthBytes"]) {
    if (b[name] !== null && b[name] !== undefined && a[name] !== null && a[name] !== undefined) {
      row[`${name}DeltaPct`] = b[name] ? Number((((a[name] - b[name]) / b[name]) * 100).toFixed(1)) : null;
    }
  }
  rows.push(row);
}
console.log(JSON.stringify({ before: beforePath, after: afterPath, rows }, null, 2));
