/* Real manager-offload preflight: same actors, login, CSRF bootstrap and report endpoints as the timed workload. */
import { manifestContext } from '../../k6/lib/payloads.js';
import { buildSeasonalPlan } from '../../k6/lib/seasonal_plan.js';
import { login } from '../../k6/lib/auth.js';
import {
  logSeasonalEvent,
  performDetailReport,
  performSummaryReport,
} from '../../k6/lib/seasonal_runtime.js';

export const options = {
  vus: 1,
  iterations: 1,
  noCookiesReset: true,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'count'],
};

const plan = buildSeasonalPlan(__ENV, manifestContext);
const phase = { phase: 'preflight', name: 'manager-offload-preflight' };

export default function () {
  const managerTarget = String(__ENV.SEASONAL_MANAGER_TARGET_URL || '').trim();
  if (!managerTarget) {
    throw new Error('SEASONAL_MANAGER_TARGET_URL is required for the real manager-offload preflight');
  }
  const warehouseActor = plan.actors.summary_warehouse;
  const monthlyActor = plan.actors.summary_monthly;
  const detailActor = plan.actors.detail_reader;
  const warehouseSession = login(managerTarget, warehouseActor.user, warehouseActor.password);
  const warehouseOk = performSummaryReport(warehouseSession, plan, phase, 'Warehouse', 0);
  const monthlySession = login(managerTarget, monthlyActor.user, monthlyActor.password);
  const monthlyOk = performSummaryReport(monthlySession, plan, phase, 'Monthly', 0);
  const detailSession = login(managerTarget, detailActor.user, detailActor.password);
  const detailOk = performDetailReport(detailSession, plan, phase);
  const ok = warehouseOk && monthlyOk && detailOk;
  logSeasonalEvent({
    event: 'manager_offload_preflight',
    warehouse_ok: warehouseOk,
    monthly_ok: monthlyOk,
    detail_ok: detailOk,
    ok,
  });
  if (!ok) {
    throw new Error('manager offload preflight report validation failed');
  }
}
