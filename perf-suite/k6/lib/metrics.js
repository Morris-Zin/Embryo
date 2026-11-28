import { Counter, Rate, Trend } from 'k6/metrics';

export const searchDuration = new Trend('search_duration', true);
export const submitDuration = new Trend('submit_duration', true);
export const reportDuration = new Trend('report_duration', true);
export const searchServerDuration = new Trend('search_server_duration', true);
export const submitServerDuration = new Trend('submit_server_duration', true);
export const reportServerDuration = new Trend('report_server_duration', true);
export const saleVouchers = new Counter('sale_vouchers');
export const transferVouchers = new Counter('transfer_vouchers');
export const errors = new Counter('errors');
export const errorRate = new Rate('error_rate');

export const seasonalVoucherStarted = new Counter('seasonal_voucher_started');
export const seasonalVoucherCompleted = new Counter('seasonal_voucher_completed');
export const seasonalVoucherRejected = new Counter('seasonal_voucher_rejected');
export const seasonalVoucherRetried = new Counter('seasonal_voucher_retried');
export const seasonalVoucherLostAck = new Counter('seasonal_voucher_lost_ack');
export const seasonalVoucherUnknown = new Counter('seasonal_voucher_unknown');
export const seasonalVoucherRecoveryReads = new Counter('seasonal_voucher_recovery_reads');
export const seasonalVoucherLinesCompleted = new Counter('seasonal_voucher_lines_completed');
export const seasonalDetailFirstRowsDuration = new Trend('seasonal_detail_first_rows_duration', true);
export const seasonalExportDuration = new Trend('seasonal_export_duration', true);
export const seasonalExportBytes = new Counter('seasonal_export_bytes');
export const seasonalExportRows = new Counter('seasonal_export_rows');
export const seasonalDetailFallbacks = new Counter('seasonal_detail_fallbacks');

export const voucherThresholds = {
  search_duration: ['p(95)<300'],
  submit_duration: ['p(95)<1500'],
  http_req_failed: ['rate<0.005'],
  errors: ['count==0'],
};

export const sharedThresholds = Object.assign({}, voucherThresholds, {
  report_duration: ['p(95)<5000'],
});

export function reportThresholdsForMode(mode) {
  return Object.assign({}, sharedThresholds, {
    'report_duration{report:Stock Balance}': ['p(95)<5000'],
    [mode === 'stock' ? 'report_duration{report:Gross Profit}' : 'report_duration{report:Fast Gross Profit}']: [
      'p(95)<5000',
    ],
  });
}

export function recordOperation(measured, uxTrend, serverTrend, tags = {}) {
  uxTrend.add(measured.endToEndDurationMs, tags);
  serverTrend.add(measured.serverDurationMs, tags);
}

export function recordOutcome(ok) {
  errorRate.add(ok ? 0 : 1);
  if (!ok) {
    errors.add(1);
  }
}
