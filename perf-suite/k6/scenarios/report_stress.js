import { sleep } from 'k6';
import exec from 'k6/execution';
import { jitterSeconds } from '../lib/latency.js';
import { reportThresholdsForMode, errors, errorRate } from '../lib/metrics.js';
import { shopWarehouseSequence } from '../lib/payloads.js';
import {
  authenticate,
  currentMode,
  performShopIteration,
  runManagerReport,
  warehouseForVu,
} from '../lib/workflow.js';

const SHOP_VUS = Number(__ENV.SHOP_VUS || 6);
const MANAGER_VUS = Number(__ENV.MANAGER_VUS || 2);
const DURATION = __ENV.DURATION || '10m';
const VOUCHERS_PER_5MIN = Number(__ENV.VOUCHERS_PER_5MIN || 40);
const SHOP_ITERATION_SECONDS = Math.max(1, (300 * SHOP_VUS) / VOUCHERS_PER_5MIN);
const REPORT_INTERVAL_SECONDS = Number(__ENV.REPORT_INTERVAL_SECONDS || 12);
const PERF_MODE = (__ENV.PERF_MODE || __ENV.MODE || 'khitmee_ops').trim().toLowerCase();

export const options = {
  noCookiesReset: true,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'count'],
  scenarios: {
    shop_workers: {
      executor: 'constant-vus',
      vus: SHOP_VUS,
      duration: DURATION,
      gracefulStop: '30s',
      exec: 'shopWorker',
    },
    report_managers: {
      executor: 'constant-vus',
      vus: MANAGER_VUS,
      duration: DURATION,
      startTime: '5s',
      gracefulStop: '30s',
      exec: 'reportManager',
    },
  },
  thresholds: reportThresholdsForMode(PERF_MODE),
};

let session;

function ensureSession() {
  if (!session) {
    session = authenticate();
  }
  return session;
}

function vuIndex() {
  return exec.vu.idInScenario || exec.vu.idInTest;
}

function recordIterationError(label, err) {
  errors.add(1);
  errorRate.add(1);
  console.error(`${label} iteration error: ${err}`);
}

export function shopWorker() {
  if (!session && exec.vu.iterationInScenario === 0) {
    sleep(((vuIndex() - 1) / Math.max(1, SHOP_VUS)) * Math.min(SHOP_ITERATION_SECONDS, 12));
  }
  const activeSession = ensureSession();
  const warehouse = warehouseForVu(vuIndex());
  try {
    performShopIteration(activeSession, warehouse, { contention: false });
  } catch (err) {
    recordIterationError('report_stress shop worker', err);
  }
  sleep(jitterSeconds(SHOP_ITERATION_SECONDS, 0.25));
}

function reportRoundRobinIndex() {
  const iteration = Math.max(0, exec.vu.iterationInScenario || 0);
  const vuOffset = Math.max(0, vuIndex() - 1);
  return iteration * Math.max(1, MANAGER_VUS) + vuOffset;
}

export function reportManager() {
  const activeSession = ensureSession();
  const mode = currentMode();
  const grossProfitName = mode === 'stock' ? 'Gross Profit' : 'Fast Gross Profit';
  const roundRobinIndex = reportRoundRobinIndex();
  const warehouses = shopWarehouseSequence();
  const warehouseIndex = roundRobinIndex % warehouses.length;
  const warehouseVisit = Math.floor(roundRobinIndex / warehouses.length);
  const warehouse = warehouses[warehouseIndex];
  const useStockBalance = (warehouseIndex + warehouseVisit) % 2 === 0;
  const reportName =
    __ENV.REPORT_NAME ||
    (useStockBalance ? 'Stock Balance' : grossProfitName);
  try {
    runManagerReport(activeSession, warehouse, { reportName });
  } catch (err) {
    recordIterationError('report manager', err);
  }
  sleep(jitterSeconds(REPORT_INTERVAL_SECONDS, 0.35));
}

export default function reportStressDefault() {
  if (exec.scenario && exec.scenario.name === 'report_managers') {
    reportManager();
  } else {
    shopWorker();
  }
}
