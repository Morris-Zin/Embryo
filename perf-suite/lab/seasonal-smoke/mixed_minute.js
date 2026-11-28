/* One real baseline minute from the seasonal plan. k6 may schedule a boundary
   iteration, so the cap is checked before login, search, read or mutation. */
import exec from 'k6/execution';
import {
  setup as discover, teardown, voucherWorker, contentionWorker, searchWorker,
  summaryWarehouseReader, summaryMonthlyReader, invoiceDetailReader,
} from '../../k6/scenarios/seasonal.js';
import { logSeasonalEvent } from '../../k6/lib/seasonal_runtime.js';

export { teardown };
export function setup() { return discover(); }
const specs = {
  baseline_vouchers: [88, 'vouchers', 88],
  baseline_contention: [1, 'contention', 1],
  baseline_searches: [80, 'searches', 80],
};
if (String(__ENV.SEASONAL_ENABLE_MANAGERS || '1').toLowerCase() !== '0') {
  Object.assign(specs, {
    summary_reader_warehouse: [6, 'warehouse', 6],
    summary_reader_monthly: [6, 'monthly', 6],
    invoice_detail_reader: [2, 'details', 2],
  });
}
const scenarios = {};
const vuOverrides = {
  baseline_vouchers: Number(__ENV.SEASONAL_VOUCHER_VUS || 0),
  baseline_searches: Number(__ENV.SEASONAL_SEARCH_VUS || 0),
};
for (const [name, [rate, fn, vus]] of Object.entries(specs)) {
  const resourceVus = vuOverrides[name] > 0 ? vuOverrides[name] : vus;
  scenarios[name] = {executor: 'constant-arrival-rate', rate, timeUnit: '1m', duration: '1m',
    exec: fn, preAllocatedVUs: resourceVus, maxVUs: resourceVus, gracefulStop: '180s'};
}
export const options = {scenarios, noCookiesReset: true, setupTimeout: '90s', teardownTimeout: '20s',
  summaryTrendStats: ['avg','min','med','max','p(95)','count']};
function bounded(callback) {
  const name = exec.scenario.name;
  const i = exec.scenario.iterationInTest;
  if (i === 0) logSeasonalEvent({event:'minute_clock', scenario:name, start_ms:exec.scenario.startTime});
  if (i >= specs[name][0]) {
    logSeasonalEvent({event:'boundary_ignored', scenario:name, index:i});
    return;
  }
  callback();
}
export function vouchers() { bounded(voucherWorker); }
export function contention() { bounded(contentionWorker); }
export function searches() { bounded(searchWorker); }
export function warehouse() { bounded(summaryWarehouseReader); }
export function monthly() { bounded(summaryMonthlyReader); }
export function details() { bounded(invoiceDetailReader); }
