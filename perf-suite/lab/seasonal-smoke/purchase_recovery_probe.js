/* Low-load targeted probe: drive exactly one baseline purchase_cycle witness
   (minute 0, sequence 0) through the production seasonal worker path with
   SEASONAL_REQUIRE_RETRY_EVIDENCE=1. Setup/teardown are the production ones,
   so the config event and discovery evidence match the real benchmark. The
   witness confirmation withholding is deliberate benchmark behavior at
   trivial load; this is split evidence, not a genuine overload lost-ack. */
import {
  setup as discover,
  teardown,
} from '../../k6/scenarios/seasonal.js';
import { manifestContext } from '../../k6/lib/payloads.js';
import { authenticate } from '../../k6/lib/workflow.js';
import { buildSeasonalPlan } from '../../k6/lib/seasonal_plan.js';
import { logSeasonalEvent, submitSeasonalVoucher } from '../../k6/lib/seasonal_runtime.js';

export { teardown };
export function setup() { return discover(); }

export const options = {
  vus: 1,
  iterations: 1,
  noCookiesReset: true,
  setupTimeout: '60s',
  teardownTimeout: '20s',
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'count'],
};

const plan = buildSeasonalPlan(__ENV, manifestContext);

export default function () {
  const actor = plan.actors.cashier;
  if (!actor) {
    throw new Error('No configured cashier actor for purchase recovery probe');
  }
  const segment = plan.segments.find((item) => item.name === 'baseline');
  if (!segment) {
    throw new Error('No baseline segment in seasonal plan');
  }
  // Map template entries exactly as the production voucherWorker does for
  // cycle 0, then select the purchase_cycle witness at minute 0 sequence 0.
  const work = segment.template.find((item) =>
    item.type === 'purchase_cycle' && item.minute === 0 && item.sequence === 0);
  if (!work) {
    throw new Error('No baseline purchase_cycle witness work item at minute 0 sequence 0');
  }
  logSeasonalEvent({ event: 'probe_witness_selected', mix_key: work.mixKey, minute: work.minute, sequence: work.sequence });
  const results = submitSeasonalVoucher(authenticate(actor.user, actor.password), plan, work);
  const pair = Array.isArray(results) ? results : [results];
  logSeasonalEvent({
    event: 'probe_finished',
    receipt_ok: Boolean(pair[0] && pair[0].ok),
    invoice_ok: Boolean(pair[1] && pair[1].ok),
  });
}
