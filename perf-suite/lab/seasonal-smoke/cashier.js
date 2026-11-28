import http from 'k6/http';
import execution from 'k6/execution';
import { sleep } from 'k6';
import { login, postMethod } from '../../k6/lib/auth.js';
import { manifestContext } from '../../k6/lib/payloads.js';
import { seasonalSettings, seasonalWarehouses } from '../../k6/lib/seasonal_plan.js';
import { buildSeasonalWorkPayload } from '../../k6/lib/seasonal_runtime.js';

const target = __ENV.TARGET_URL || 'http://127.0.0.1:18265';
const settings = seasonalSettings({SEASONAL_PROFILE: 'smoke', RUN_ID: __ENV.CASHIER_RUN});
settings.runId = __ENV.CASHIER_RUN;
settings.seed = __ENV.CASHIER_SEED || '20240601';
const strict = __ENV.CASHIER_STRICT === '1';
const plan = {settings, warehouses: seasonalWarehouses(manifestContext)};
const groups = {small: [60, 10, 10], medium: [16, 50, 5], large: [4, 200, 3]};
const scenarios = Object.fromEntries(Object.entries(groups).map(([key, [rate, count, vus]]) => [key, {
  executor: 'constant-arrival-rate', rate, timeUnit: '1m', duration: '1m',
  preAllocatedVUs: vus, maxVUs: vus, gracefulStop: '130s', exec: 'sale',
}]));
export const options = {scenarios, noCookiesReset: true, setupTimeout: '60s'};

function event(data) {
  console.log('CASHIER ' + JSON.stringify({...data, utc: new Date().toISOString()}));
}

export function setup() {
  const session = login(target, __ENV.PERF_USER, __ENV.PERF_PASSWORD);
  return {session};
}

export function sale({session}) {
  const group = execution.scenario.name;
  const sequence = execution.scenario.iterationInTest;
  const lineCount = groups[group][1];
  const id = `SCS-${settings.runId}-${group}-${sequence}`;
  const elapsed = () => Date.now() - execution.scenario.startTime;
  if (strict && sequence >= groups[group][0]) {
    event({type: 'boundary_ignored', group, sequence, elapsed_ms: elapsed()});
    return;
  }
  event({type: 'arrival', id, group, lineCount, elapsed_ms: elapsed()});
  try {
    http.cookieJar().set(target, 'sid', session.sid);
    const work = {type: 'sale', mixKey: group, segment: 'single_minute', minute: 0, sequence, lineCount};
    const built = buildSeasonalWorkPayload(plan, work);
    const payload = {client_voucher_id: id, warehouse: built.warehouse, customer: built.payload.customer,
      price_list: built.payload.price_list, payment_mode: built.payload.payment_mode, items: built.lines};
    event({type: 'submit', id, warehouse: built.warehouse, items: built.lines.map(i => [i.item_code, i.qty])});
    const started = Date.now();
    const attempt = () => {
      sleep(0.05);
      return postMethod(session, 'khitmee_ops.api.vouchers.submit_sale', payload,
        {timeout: '120s', tags: {name: 'cashier_submit', group}});
    };
    let response = attempt();
    const lostAck = strict && group === 'small' && sequence === 0 && response.status === 200;
    if (strict && (lostAck || response.status === 0 || response.status >= 500)) {
      const originalVoucher = lostAck ? response.json('message.voucher_no') : null;
      event({type: 'retry', id, lost_ack: lostAck, original_voucher: originalVoucher, status: response.status});
      response = attempt();
    }
    let body = {};
    try { body = response.json(); } catch (_) { /* HTTP transport failures can have no body. */ }
    const message = body.message || {};
    const confirmed = response.status === 200 && message.doctype === 'Sales Invoice' && !!message.voucher_no
      && Array.isArray(message.items) && message.items.length === lineCount;
    event({type: confirmed ? 'confirmed' : 'failed', id, status: response.status, elapsed_ms: elapsed(),
      duration_ms: Date.now() - started, http_ms: response.timings.duration, voucher: message.voucher_no || null,
      http_waiting_ms: response.timings.waiting, http_sending_ms: response.timings.sending,
      http_receiving_ms: response.timings.receiving,
      reason: confirmed ? null : (body.exc_type || response.error || 'invalid sale confirmation'),
      detail: confirmed ? null : String(body.exception || '').slice(0, 240)});
  } catch (error) {
    event({type: 'failed', id, reason: String(error), elapsed_ms: elapsed()});
  }
}

export function handleSummary(summary) {
  const values = name => (summary.metrics[name] || {}).values || {};
  event({type: 'summary', iterations: values('iterations').count || 0,
    dropped: values('dropped_iterations').count || 0, http_requests: values('http_reqs').count || 0});
  return {};
}
