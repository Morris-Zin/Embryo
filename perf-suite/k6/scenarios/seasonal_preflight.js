import { errorRate, errors } from '../lib/metrics.js';
import { manifestContext } from '../lib/payloads.js';
import { authenticate, currentMode } from '../lib/workflow.js';
import { buildSeasonalPlan, publicPlanConfig } from '../lib/seasonal_plan.js';
import {
  logSeasonalEvent,
  performCashierDiscovery,
  performDetailReport,
  performExportCycle,
  performSeasonalSearch,
  performSummaryReport,
  submitSeasonalVoucher,
  verifyUnauthorizedDetailDenied,
} from '../lib/seasonal_runtime.js';

if (currentMode() === 'stock') {
  throw new Error('The seasonal preflight qualifies the Khitmee counter paths.');
}

const plan = new SharedArraylessPlan();
function SharedArraylessPlan() {
  return buildSeasonalPlan(__ENV, manifestContext);
}

const preflightPlan = Object.assign({}, plan, {
  settings: Object.assign({}, plan.settings, { detailPageCount: Math.max(10, plan.settings.detailPageCount) }),
});

export const options = {
  noCookiesReset: true,
  scenarios: {
    idle_preflight: {
      executor: 'shared-iterations',
      vus: 1,
      iterations: 1,
      maxDuration: '20m',
      exec: 'idlePreflight',
    },
  },
  thresholds: {
    errors: ['count==0'],
  },
};

function actorSession(actorKey) {
  const actor = preflightPlan.actors[actorKey];
  if (!actor) {
    throw new Error(`missing preflight actor ${actorKey}`);
  }
  return authenticate(actor.user, actor.password);
}

function templateOf(type) {
  const segment = preflightPlan.segments[0];
  const work = segment.template.find((entry) => entry.type === type || (type === 'purchase_cycle' && entry.type === 'purchase_cycle'));
  if (!work) {
    throw new Error(`seasonal template lacks ${type}`);
  }
  return Object.assign({}, work, { minute: 0, sequence: 0, segment: `${segment.name}_preflight` });
}

function markFailure(label, detail) {
  errors.add(1);
  errorRate.add(1);
  logSeasonalEvent({ event: 'preflight_step_failure', step: label, reason: String(detail || 'failed') });
}

function check(results, label, value) {
  results[label] = Boolean(value);
  if (!results[label]) {
    markFailure(label, 'returned false');
  }
}

export function setup() {
  logSeasonalEvent({ event: 'config', config: publicPlanConfig(preflightPlan) });
  return { preflight: true };
}

export function idlePreflight() {
  const results = {};
  const phase = Object.assign({}, preflightPlan.segments[0], { name: 'idle_preflight', phase: 'baseline' });
  try {
    const cashier = actorSession('cashier');
    check(results, 'login_boot_and_discovery', performCashierDiscovery(cashier, preflightPlan));
    check(results, 'search', performSeasonalSearch(cashier, preflightPlan, phase, 1));

    const sale = submitSeasonalVoucher(cashier, preflightPlan, templateOf('sale'));
    check(results, 'sale', sale && sale.ok);
    const transfer = submitSeasonalVoucher(cashier, preflightPlan, templateOf('transfer'));
    check(results, 'transfer', transfer && transfer.ok);
    const purchase = submitSeasonalVoucher(cashier, preflightPlan, templateOf('purchase_cycle'));
    const purchaseRows = Array.isArray(purchase) ? purchase : [];
    check(results, 'purchase_receipt', purchaseRows[0] && purchaseRows[0].ok);
    check(results, 'purchase_invoice', purchaseRows[1] && purchaseRows[1].ok);

    const warehouse = actorSession('summary_warehouse');
    performSummaryReport(warehouse, preflightPlan, phase, 'Warehouse', 0);
    check(results, 'warm_summary_warehouse', performSummaryReport(warehouse, preflightPlan, phase, 'Warehouse', 1));
    const monthly = actorSession('summary_monthly');
    performSummaryReport(monthly, preflightPlan, phase, 'Monthly', 0);
    check(results, 'warm_summary_monthly', performSummaryReport(monthly, preflightPlan, phase, 'Monthly', 1));

    const detail = actorSession('detail_reader');
    check(results, 'detail_page_1_and_deep_page', performDetailReport(detail, preflightPlan, phase));
    check(results, 'unauthorized_detail_denied', verifyUnauthorizedDetailDenied(cashier, preflightPlan));

    const exportActor = actorSession('export_requester');
    const exportResult = performExportCycle(exportActor, preflightPlan, phase);
    check(results, 'export_enqueue_status_download', exportResult && exportResult.ok);
  } catch (err) {
    markFailure('preflight_exception', err && err.message ? err.message : err);
  }
  const failed = Object.entries(results).filter(([, ok]) => !ok).map(([name]) => name);
  const missing = [
    'login_boot_and_discovery', 'sale', 'transfer', 'purchase_receipt', 'purchase_invoice', 'search',
    'warm_summary_warehouse', 'warm_summary_monthly', 'detail_page_1_and_deep_page',
    'unauthorized_detail_denied', 'export_enqueue_status_download',
  ].filter((name) => !(name in results));
  const passed = failed.length === 0 && missing.length === 0 && !results.preflight_exception;
  logSeasonalEvent({
    event: 'preflight_result',
    passed,
    results,
    failed_steps: failed,
    missing_steps: missing,
    mode: 'idle single request of each type through nginx',
  });
  if (!passed) {
    errors.add(1);
    errorRate.add(1);
    throw new Error(`idle preflight failed: failed=${failed.join(',') || 'none'} missing=${missing.join(',') || 'none'}`);
  }
}

export function teardown() {
  logSeasonalEvent({ event: 'preflight_finished' });
}
