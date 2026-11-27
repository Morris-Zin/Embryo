import { sleep } from 'k6';
import exec from 'k6/execution';
import { jitterSeconds } from '../lib/latency.js';
import { voucherThresholds, errors, errorRate } from '../lib/metrics.js';
import { authenticate, performShopIteration, warehouseForVu } from '../lib/workflow.js';

const SHOP_VUS = Number(__ENV.SHOP_VUS || 6);
const VOUCHERS_PER_5MIN = Number(__ENV.VOUCHERS_PER_5MIN || 40);
const ITERATION_SECONDS = Math.max(1, (300 * SHOP_VUS) / VOUCHERS_PER_5MIN);

export const options = {
  noCookiesReset: true,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'count'],
  scenarios: {
    june_sustained: {
      executor: 'constant-vus',
      vus: SHOP_VUS,
      duration: __ENV.DURATION || '10m',
      gracefulStop: '30s',
    },
  },
  thresholds: voucherThresholds,
};

let session;

function vuIndex() {
  return exec.vu.idInScenario || exec.vu.idInTest;
}

export default function juneSustained() {
  if (!session) {
    if (exec.vu.iterationInScenario === 0) {
      sleep(((vuIndex() - 1) / Math.max(1, SHOP_VUS)) * Math.min(ITERATION_SECONDS, 12));
    }
    session = authenticate();
  }
  const warehouse = warehouseForVu(vuIndex());
  try {
    performShopIteration(session, warehouse, { contention: false });
  } catch (err) {
    errors.add(1);
    errorRate.add(1);
    console.error(`june_sustained iteration error at ${warehouse}: ${err}`);
  }
  sleep(jitterSeconds(ITERATION_SECONDS, 0.25));
}
