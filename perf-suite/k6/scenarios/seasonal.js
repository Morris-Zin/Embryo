import { sleep } from 'k6';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { errorRate, errors } from '../lib/metrics.js';
import { manifestContext } from '../lib/payloads.js';
import { authenticate, currentMode } from '../lib/workflow.js';
import { login } from '../lib/auth.js';
import { buildSeasonalPlan, formatDurationSeconds, publicPlanConfig } from '../lib/seasonal_plan.js';
import {
  logSeasonalEvent,
  performCashierDiscovery,
  performContentionBurst,
  performDetailReport,
  performExportCycle,
  performSeasonalSearch,
  performSummaryReport,
  phaseAt,
  submitSeasonalVoucher,
} from '../lib/seasonal_runtime.js';

if (currentMode() === 'stock') {
  throw new Error('The seasonal acceptance benchmark qualifies the Khitmee counter paths; use the legacy scenarios for stock-mode comparisons.');
}

const plan = new SharedArray('seasonal_plan', function () {
  return [buildSeasonalPlan(__ENV, manifestContext)];
})[0];
const settings = plan.settings;

function arrivalScenario(segment, execName, rate, preAllocatedVUs, maxVUs) {
  return {
    executor: 'constant-arrival-rate',
    rate: rate.rate,
    timeUnit: rate.timeUnit,
    duration: segment.duration,
    startTime: formatDurationSeconds(segment.startSeconds),
    preAllocatedVUs,
    maxVUs,
    gracefulStop: '30s',
    exec: execName,
  };
}

function buildScenarios() {
  const scenarios = {};
  plan.segments.forEach((segment) => {
    scenarios[`${segment.name}_vouchers`] = arrivalScenario(
      segment,
      'voucherWorker',
      segment.regularRate,
      settings.voucherPreAllocatedVus,
      settings.voucherMaxVus
    );
    scenarios[`${segment.name}_contention`] = arrivalScenario(segment, 'contentionWorker', segment.contentionRate, 1, 2);
    scenarios[`${segment.name}_searches`] = arrivalScenario(
      segment,
      'searchWorker',
      segment.searchRate,
      settings.searchPreAllocatedVus,
      settings.searchMaxVus
    );
  });
  if (!settings.enableManagers) {
    return scenarios;
  }
  const totalDuration = formatDurationSeconds(plan.totalDurationSeconds);
  scenarios.summary_reader_warehouse = {
    executor: 'constant-arrival-rate',
    rate: 6,
    timeUnit: '1m',
    duration: totalDuration,
    startTime: '0s',
    preAllocatedVUs: 1,
    maxVUs: 2,
    gracefulStop: '10s',
    exec: 'summaryWarehouseReader',
  };
  scenarios.summary_reader_monthly = {
    executor: 'constant-arrival-rate',
    rate: 6,
    timeUnit: '1m',
    duration: totalDuration,
    startTime: '0s',
    preAllocatedVUs: 1,
    maxVUs: 2,
    gracefulStop: '10s',
    exec: 'summaryMonthlyReader',
  };
  scenarios.invoice_detail_reader = {
    executor: 'constant-arrival-rate',
    rate: 2,
    timeUnit: '1m',
    duration: totalDuration,
    startTime: '0s',
    preAllocatedVUs: 1,
    maxVUs: 2,
    gracefulStop: '35s',
    exec: 'invoiceDetailReader',
  };
  if (settings.enableExport) {
    scenarios.full_year_export = {
      executor: 'constant-vus',
      vus: 1,
      duration: totalDuration,
      startTime: '0s',
      gracefulStop: '10s',
      exec: 'fullYearExportWorker',
    };
  }
  return scenarios;
}

function endpointAttributionThresholds() {
  // k6 materializes tagged submetrics only when a threshold references them.
  // These non-failing counts make the load generator's own per-class HTTP
  // request totals available in the exported summary for attribution coverage.
  const classes = ['cashier_submit', 'search', 'login/bootstrap', 'manager_summary', 'manager_detail', 'manager_export', 'unclassified'];
  const thresholds = {};
  classes.forEach((endpointClass) => {
    thresholds[`http_reqs{endpoint_class:${endpointClass}}`] = ['count>=0'];
  });
  return thresholds;
}

function phaseThresholds() {
  const thresholds = {};
  ['baseline', 'margin', 'recovery'].forEach((phase) => {
    thresholds[`search_duration{phase:${phase}}`] = ['p(95)<300'];
    thresholds[`submit_duration{phase:${phase},voucher_type:sale}`] = ['p(95)<1500'];
    thresholds[`submit_duration{phase:${phase},voucher_type:transfer}`] = ['p(95)<1500'];
    thresholds[`submit_duration{phase:${phase},voucher_type:purchase_receipt}`] = ['p(95)<1500'];
    thresholds[`submit_duration{phase:${phase},voucher_type:purchase_invoice}`] = ['p(95)<1500'];
    thresholds[`submit_duration{phase:${phase},voucher_type:sale,line_count:200}`] = ['p(95)<1500'];
    if (settings.enableManagers) {
      thresholds[`seasonal_detail_first_rows_duration{phase:${phase}}`] = ['p(95)<2000'];
      // Manager errors are tagged by endpoint class and phase. Core phases have
      // no designed manager rejection; the overload phase is deliberately absent
      // because cashier admission/backpressure failures are the designed result there.
      ['manager_summary', 'manager_detail', 'manager_export'].forEach((endpointClass) => {
        thresholds[`errors{endpoint_class:${endpointClass},phase:${phase}}`] = ['count==0'];
      });
    }
  });
  return thresholds;
}

export const options = {
  noCookiesReset: true,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'count'],
  setupTimeout: '60s',
  teardownTimeout: '30s',
  scenarios: buildScenarios(),
  thresholds: Object.assign(endpointAttributionThresholds(), phaseThresholds()),
};

let session;
let sessionActor;

function ensureState() {
  const scenarioActors = {
    summary_reader_warehouse: 'summary_warehouse',
    summary_reader_monthly: 'summary_monthly',
    invoice_detail_reader: 'detail_reader',
    full_year_export: 'export_requester',
  };
  const actorKey = scenarioActors[exec.scenario.name] || 'cashier';
  if (!session || actorKey !== sessionActor) {
    const actor = plan.actors[actorKey];
    if (!actor) {
      throw new Error(`No configured seasonal actor '${actorKey}' for scenario '${exec.scenario.name}'`);
    }
    const managerTarget = String(__ENV.SEASONAL_MANAGER_TARGET_URL || '').trim();
    session = actorKey !== 'cashier' && managerTarget
      ? login(managerTarget, actor.user, actor.password)
      : authenticate(actor.user, actor.password);
    sessionActor = actorKey;
  }
  return session;
}

function noteIterationError(label, err, tags = {}) {
  errors.add(1, tags);
  errorRate.add(1, tags);
  console.error(`${label}: ${err && err.message ? err.message : err}`);
}

function phaseEvidenceFallback() {
  try {
    return currentPhase();
  } catch (_err) {
    return { phase: 'unknown', name: 'unknown' };
  }
}

function noteManagerIterationError(label, rejectEvent, err, extra = {}, phaseEvidence = null) {
  // Fold iterator exceptions into the same endpoint-specific terminal failure
  // evidence used by the evaluator. Otherwise a login/report exception would
  // increment the opaque errors counter without contributing to that endpoint's
  // attempted-request denominator.
  const phase = phaseEvidence || phaseEvidenceFallback();
  const endpointClass = {
    manager_summary_reject: 'manager_summary',
    manager_detail_reject: 'manager_detail',
    export_reject: 'manager_export',
  }[rejectEvent] || 'manager_unknown';
  noteIterationError(label, err, { phase: phase.phase, endpoint_class: endpointClass });
  logSeasonalEvent(Object.assign({
    event: rejectEvent,
    phase: phase.phase,
    segment: phase.name,
    reason: `${label}: ${err && err.message ? err.message : err}`,
  }, extra));
}

function exactIteration() {
  const iteration = exec.scenario ? exec.scenario.iterationInTest : undefined;
  if (!Number.isInteger(iteration) || iteration < 0) {
    throw new Error(`k6 did not expose an exact non-negative iterationInTest for scenario '${exec.scenario && exec.scenario.name}'`);
  }
  return iteration;
}

function segmentFromScenario(prefix) {
  const name = exec.scenario && exec.scenario.name ? exec.scenario.name : '';
  const segmentName = name.endsWith(prefix) ? name.slice(0, -prefix.length) : name;
  const found = plan.segments.find((segment) => segment.name === segmentName);
  if (!found) {
    throw new Error(`No seasonal segment found for k6 scenario '${name}'`);
  }
  return found;
}

export function voucherWorker() {
  const segment = segmentFromScenario('_vouchers');
  const iteration = exactIteration();
  const templateLength = Math.max(1, segment.template.length);
  const cycle = Math.floor(iteration / templateLength);
  const base = segment.template[iteration % templateLength];
  const work = Object.assign({}, base, {
    minute: base.minute + cycle * segment.scaleDen,
    cycle,
  });
  try {
    logSeasonalEvent({
      event: 'voucher_iteration_entry', phase: work.phase, segment: work.segment,
      type: work.type, mix_key: work.mixKey, line_count: work.lineCount,
      minute: work.minute, sequence: work.sequence, iteration,
    });
    const actorSession = ensureState();
    logSeasonalEvent({
      event: 'voucher_actor_ready', phase: work.phase, segment: work.segment,
      type: work.type, mix_key: work.mixKey, line_count: work.lineCount,
      minute: work.minute, sequence: work.sequence, iteration,
    });
    submitSeasonalVoucher(actorSession, plan, work);
  } catch (err) {
    noteIterationError('seasonal voucher worker', err);
  }
}

function minuteAllocatedCount(perMinute, minute, scaleNum, scaleDen) {
  const end = Math.floor(((minute + 1) * perMinute * scaleNum) / scaleDen);
  const start = Math.floor((minute * perMinute * scaleNum) / scaleDen);
  return end - start;
}

function contentionWork(segment, iteration) {
  const iterationsPerBlock = Math.max(1, segment.scaleNum);
  const block = Math.floor(iteration / iterationsPerBlock);
  const withinBlock = iteration % iterationsPerBlock;
  let cumulative = 0;
  for (let minute = 0; minute < segment.scaleDen; minute += 1) {
    const count = minuteAllocatedCount(1, minute, segment.scaleNum, segment.scaleDen);
    if (withinBlock < cumulative + count) {
      return {
        phase: segment.phase,
        segment: segment.name,
        overloadStep: segment.overloadStep,
        minute: block * segment.scaleDen + minute,
        sequence: withinBlock - cumulative,
        cycle: block,
        contention: true,
      };
    }
    cumulative += count;
  }
  throw new Error(`Unable to map exact contention iteration ${iteration} for ${segment.name}`);
}

export function contentionWorker() {
  const segment = segmentFromScenario('_contention');
  const work = contentionWork(segment, exactIteration());
  try {
    performContentionBurst(ensureState(), plan, work);
  } catch (err) {
    noteIterationError('seasonal contention worker', err);
  }
}

export function searchWorker() {
  const segment = segmentFromScenario('_searches');
  try {
    performSeasonalSearch(ensureState(), plan, segment, exactIteration());
  } catch (err) {
    noteIterationError('seasonal search worker', err);
  }
}

function currentPhase() {
  // All manager scenarios start at t=0. A late-starting VU shares this clock.
  return phaseAt(plan, (Date.now() - exec.scenario.startTime) / 1000);
}

export function summaryWarehouseReader() {
  const iterationPhase = currentPhase();
  try {
    performSummaryReport(ensureState(), plan, iterationPhase, 'Warehouse', exactIteration());
  } catch (err) {
    noteManagerIterationError('warehouse summary reader', 'manager_summary_reject', err, { group_by: 'Warehouse', actor: 'summary_warehouse' }, iterationPhase);
  }
}

export function summaryMonthlyReader() {
  const iterationPhase = currentPhase();
  try {
    performSummaryReport(ensureState(), plan, iterationPhase, 'Monthly', exactIteration());
  } catch (err) {
    noteManagerIterationError('monthly summary reader', 'manager_summary_reject', err, { group_by: 'Monthly', actor: 'summary_monthly' }, iterationPhase);
  }
}

export function invoiceDetailReader() {
  const iterationPhase = currentPhase();
  try {
    performDetailReport(ensureState(), plan, iterationPhase, exactIteration());
  } catch (err) {
    noteManagerIterationError('invoice detail reader', 'manager_detail_reject', err, { actor: 'detail_reader' }, iterationPhase);
  }
}

export function fullYearExportWorker() {
  const iterationPhase = currentPhase();
  try {
    performExportCycle(ensureState(), plan, iterationPhase);
  } catch (err) {
    noteManagerIterationError('full-year export worker', 'export_reject', err, { actor: 'export_requester' }, iterationPhase);
    sleep(5);
  }
}

export function setup() {
  logSeasonalEvent({ event: 'config', config: publicPlanConfig(plan) });
  // The summary's HTTP counters include setup/discovery requests, so the
  // attribution evidence window starts before them rather than only before
  // the scheduled load. This keeps the k6 denominator and nginx log window aligned.
  logSeasonalEvent({ event: 'measured_window_start' });
  const setupSession = authenticate(plan.actors.cashier.user, plan.actors.cashier.password);
  if (!performCashierDiscovery(setupSession, plan)) {
    throw new Error('cashier discovery failed against the real login/search path; refusing to start voucher mutation');
  }
  return { cashier_discovery: true };
}

export function teardown() {
  logSeasonalEvent({ event: 'measured_window_end' });
  logSeasonalEvent({ event: 'load_generator_finished' });
}
