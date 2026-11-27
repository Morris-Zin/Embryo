import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Trend } from 'k6/metrics';
import { postMethod } from '../lib/auth.js';
import { jitterSeconds, measureCall } from '../lib/latency.js';
import { errorRate, errors, recordOperation, voucherThresholds } from '../lib/metrics.js';
import { manifestContext } from '../lib/payloads.js';
import {
  authenticate,
  currentMode,
  performShopIteration,
  warehouseForVu,
} from '../lib/workflow.js';

const SHOP_VUS = Number(__ENV.SHOP_VUS || 6);
const MANAGER_VUS = Math.max(3, Number(__ENV.MANAGER_VUS || 3));
const DURATION = __ENV.DURATION || '10m';
const SHOP_ITERATION_SECONDS = Number(__ENV.SHOP_ITERATION_SECONDS || 8);
const REPORT_INTERVAL_SECONDS = Number(__ENV.REPORT_INTERVAL_SECONDS || 3);
const REPORT_TIMEOUT = __ENV.REPORT_TIMEOUT || '30s';

const profitSummary1yDuration = new Trend('profit_summary_1y_duration', true);
const profitSummary1yServerDuration = new Trend('profit_summary_1y_server_duration', true);

export const options = {
  noCookiesReset: true,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'count'],
  scenarios: {
    counter_workers: {
      executor: 'constant-vus',
      vus: SHOP_VUS,
      duration: DURATION,
      gracefulStop: '30s',
      exec: 'counterWorker',
    },
    profit_summary_managers: {
      executor: 'constant-vus',
      vus: MANAGER_VUS,
      duration: DURATION,
      startTime: '3s',
      gracefulStop: '30s',
      exec: 'profitSummaryManager',
    },
  },
  thresholds: Object.assign({}, voucherThresholds, {
    profit_summary_1y_duration: ['p(95)<500'],
    profit_summary_1y_server_duration: ['p(95)<500'],
  }),
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

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function shiftIsoDays(isoDate, days) {
  const date = new Date(`${isoDate}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function oneYearRange() {
  const reportWindow = manifestContext.reportWindow || {};
  const toDate = __ENV.PROFIT_TO_DATE || __ENV.REPORT_TO_DATE || reportWindow.to_date || todayIso();
  const fromDate = __ENV.PROFIT_FROM_DATE || __ENV.REPORT_FROM_DATE || shiftIsoDays(toDate, -365);
  return { fromDate, toDate };
}

function parseJson(response) {
  try {
    return { ok: true, data: response.json() };
  } catch (err) {
    return { ok: false, error: err && err.message ? err.message : String(err) };
  }
}

function responseObject(parsed) {
  return parsed.ok && parsed.data && typeof parsed.data === 'object' ? parsed.data : {};
}

function responseFailed(response) {
  if (!response || response.status < 200 || response.status >= 400) {
    return true;
  }
  const parsed = parseJson(response);
  if (!parsed.ok) {
    return true;
  }
  const body = responseObject(parsed);
  return Boolean(body.exc || body.exception || body.exc_type || body.success === false || body.status === 'error');
}

function reportNameForMode() {
  return currentMode() === 'stock' ? 'Gross Profit' : 'Fast Gross Profit';
}

function managerRoundIndex() {
  const iteration = Math.max(0, exec.vu.iterationInScenario || 0);
  const vuOffset = Math.max(0, vuIndex() - 1);
  return iteration * MANAGER_VUS + vuOffset;
}

function profitSummaryFilters(groupBy) {
  const range = oneYearRange();
  return {
    company: __ENV.COMPANY || manifestContext.company,
    from_date: range.fromDate,
    to_date: range.toDate,
    group_by: groupBy,
    include_returned_invoices: Number(__ENV.INCLUDE_RETURNED_INVOICES || 1),
  };
}

function recordFailure(label, response, groupBy) {
  errors.add(1);
  errorRate.add(1);
  const parsed = parseJson(response);
  const body = responseObject(parsed);
  console.error(
    JSON.stringify({
      event: 'profit_summary_1y_failure',
      label,
      group_by: groupBy,
      status: response && response.status !== undefined ? response.status : null,
      exc_type: body.exc_type || null,
      message: typeof body.message === 'string' ? body.message.slice(0, 160) : null,
    })
  );
}

function runProfitSummary1y(activeSession, groupBy) {
  const reportName = __ENV.REPORT_NAME || reportNameForMode();
  const filters = profitSummaryFilters(groupBy);
  const payload = {
    report_name: reportName,
    filters: JSON.stringify(filters),
    ignore_prepared_report: 1,
  };
  const measured = measureCall(
    () => [
      postMethod(activeSession, 'frappe.desk.query_report.run', payload, {
        asForm: true,
        timeout: REPORT_TIMEOUT,
        tags: {
          name: 'query_report',
          endpoint: 'query_report',
          report: reportName,
          group_by: groupBy,
          range: '1y',
        },
      }),
    ],
    { roundTrips: 1 }
  );
  const response = measured.value[0];
  const parsed = parseJson(response);
  const body = responseObject(parsed);
  const message = body.message && typeof body.message === 'object' ? body.message : null;
  const ok =
    !responseFailed(response) &&
    Boolean(message) &&
    Array.isArray(message.result) &&
    // Fast Gross Profit returns an empty result when the request is pending or over
    // budget; that is not a completed 1-year summary.
    message.result.length > 0 &&
    !message.error &&
    !message.prepared_report &&
    !message.prepare_report;

  check(response, {
    '1-year profit summary completed synchronously': () => ok,
  });
  if (!ok) {
    recordFailure('profit_summary_1y', response, groupBy);
    return { ok, groupBy, filters };
  }

  recordOperation(measured, profitSummary1yDuration, profitSummary1yServerDuration, {
    report: reportName,
    group_by: groupBy,
    range: '1y',
  });
  return { ok, groupBy, filters, rows: message.result.length };
}

export function counterWorker() {
  if (!session && exec.vu.iterationInScenario === 0) {
    sleep(((vuIndex() - 1) / Math.max(1, SHOP_VUS)) * Math.min(SHOP_ITERATION_SECONDS, 6));
  }
  const activeSession = ensureSession();
  try {
    performShopIteration(activeSession, warehouseForVu(vuIndex()), { contention: false });
  } catch (err) {
    errors.add(1);
    errorRate.add(1);
    console.error(`profit_summary_1y counter worker error: ${err}`);
  }
  sleep(jitterSeconds(SHOP_ITERATION_SECONDS, 0.25));
}

export function profitSummaryManager() {
  const activeSession = ensureSession();
  const groupBy = managerRoundIndex() % 2 === 0 ? 'Warehouse' : 'Monthly';
  try {
    runProfitSummary1y(activeSession, groupBy);
  } catch (err) {
    errors.add(1);
    errorRate.add(1);
    console.error(`profit_summary_1y manager error: ${err}`);
  }
  sleep(jitterSeconds(REPORT_INTERVAL_SECONDS, 0.3));
}

export default function profitSummary1yDefault() {
  if (exec.scenario && exec.scenario.name === 'profit_summary_managers') {
    profitSummaryManager();
  } else {
    counterWorker();
  }
}
