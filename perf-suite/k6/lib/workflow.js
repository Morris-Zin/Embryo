import { check } from 'k6';
import {
  assertSafeTarget,
  DEFAULT_TARGET_URL,
  getMethod,
  login,
  postMethod,
} from './auth.js';
import { measureCall } from './latency.js';
import {
  errorRate,
  errors,
  recordOperation,
  recordOutcome,
  reportDuration,
  reportServerDuration,
  saleVouchers,
  searchDuration,
  searchServerDuration,
  submitDuration,
  submitServerDuration,
  transferVouchers,
} from './metrics.js';
import {
  buildKhitmeeSalePayload,
  buildKhitmeeTransferPayload,
  buildStockSaleFlow,
  buildStockTransferFlow,
  chooseTransferTarget,
  manifestContext,
  pickItem,
  randomItem,
  searchTermForItem,
  shopWarehouseSequence,
  warehouseRole,
} from './payloads.js';

export function currentMode() {
  return String(__ENV.PERF_MODE || __ENV.MODE || 'khitmee_ops').trim().toLowerCase() === 'stock'
    ? 'stock'
    : 'khitmee_ops';
}

export function targetUrl() {
  return assertSafeTarget(__ENV.TARGET_URL || DEFAULT_TARGET_URL);
}

export function authenticate() {
  const user = __ENV.PERF_USER || __ENV.FRAPPE_USER || 'Administrator';
  const password = __ENV.PERF_PASSWORD || __ENV.FRAPPE_PASSWORD || '';
  return login(targetUrl(), user, password);
}

export function parseJsonBody(response) {
  if (!response) {
    return { ok: false, error: 'missing response' };
  }
  try {
    return { ok: true, data: response.json() };
  } catch (err) {
    return { ok: false, error: err && err.message ? err.message : String(err) };
  }
}

function responseObject(parsed) {
  return parsed.ok && parsed.data && typeof parsed.data === 'object' ? parsed.data : {};
}

function responseOk(response) {
  if (!response || response.status < 200 || response.status >= 400) {
    return false;
  }
  const parsed = parseJsonBody(response);
  if (!parsed.ok) {
    return false;
  }
  const body = responseObject(parsed);
  if (body.exc || body.exception || body.exc_type) {
    return false;
  }
  if (body.success === false || body.status === 'error' || body.error === true) {
    return false;
  }
  return true;
}

const FAILURE_LOG_LIMIT_PER_VU = 20;
const failureLogCounts = {};

function normalizeMessageValue(value, depth = 0) {
  if (value === null || value === undefined) {
    return '';
  }
  if (depth > 4) {
    return String(value);
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed.length > 1 && (trimmed[0] === '[' || trimmed[0] === '{')) {
      let reparsed;
      let parseFailed = false;
      try {
        reparsed = JSON.parse(trimmed);
      } catch (_err) {
        parseFailed = true;
      }
      if (!parseFailed) {
        return normalizeMessageValue(reparsed, depth + 1);
      }
    }
    return trimmed;
  }
  if (Array.isArray(value)) {
    return value
      .map((entry) => normalizeMessageValue(entry, depth + 1))
      .filter(Boolean)
      .join(' | ');
  }
  if (typeof value === 'object') {
    for (const key of ['message', 'error', 'exception', 'exc']) {
      const nested = normalizeMessageValue(value[key], depth + 1);
      if (nested) {
        return nested;
      }
    }
    try {
      return JSON.stringify(value);
    } catch (_err) {
      return String(value);
    }
  }
  return String(value);
}

function failureMessageExcerpt(parsed, response) {
  let text = '';
  if (parsed.ok) {
    const body = responseObject(parsed);
    for (const key of ['_server_messages', 'exception', 'message', 'exc']) {
      text = normalizeMessageValue(body[key]);
      if (text) {
        break;
      }
    }
  }
  if (!text) {
    if (!parsed.ok) {
      if (response && response.body) {
        text = String(response.body);
      } else if (parsed.error) {
        text = String(parsed.error);
      }
    } else if (response && response.body) {
      text = String(response.body);
    }
  }
  return text.replace(/\s+/g, ' ').trim().slice(0, 180);
}

function firstFailedResponse(responses) {
  const list = responses || [];
  for (let index = 0; index < list.length; index += 1) {
    if (!responseOk(list[index])) {
      return list[index];
    }
  }
  return list.length ? list[list.length - 1] : null;
}

function skuQtysFromItems(items) {
  return (items || []).slice(0, 3).map((line) => `${line.item_code}:${line.qty}`);
}

function logK6Failure(details) {
  const vu = typeof __VU === 'undefined' ? 'init' : String(__VU);
  const alreadyLogged = failureLogCounts[vu] || 0;
  if (alreadyLogged >= FAILURE_LOG_LIMIT_PER_VU) {
    return;
  }
  failureLogCounts[vu] = alreadyLogged + 1;

  const response = firstFailedResponse(details.responses);
  const parsed = parseJsonBody(response);
  const body = responseObject(parsed);
  console.error(
    JSON.stringify({
      event: 'k6_failure',
      op: details.op || null,
      status: response && response.status !== undefined ? response.status : null,
      exc_type: body.exc_type || null,
      warehouse: details.warehouse || null,
      target_warehouse: details.targetWarehouse || null,
      report_name: details.reportName || null,
      voucher_id: details.voucherId || null,
      sku_qtys: (details.skuQtys || []).slice(0, 3),
      msg_excerpt: failureMessageExcerpt(parsed, response),
    })
  );
}

function allResponsesOk(responses) {
  return responses.every(responseOk);
}

function finalResponse(responses) {
  return responses[responses.length - 1];
}

function recordLogicalOutcome(ok, responses, label) {
  recordOutcome(ok);
  check(finalResponse(responses), {
    [`${label} succeeded`]: () => ok,
  });
}

export function performSearch(session, warehouse, options = {}) {
  const exact = Boolean(options.exact);
  const item = pickItem({ contention: options.contention });
  const term = options.term || searchTermForItem(item, exact);
  const mode = currentMode();
  let measured;

  let barcodeLookup = false;
  if (mode === 'stock') {
    const useBarcode = exact || (item.barcode && term === item.barcode);
    if (useBarcode) {
      barcodeLookup = true;
      measured = measureCall(
        () => [
          postMethod(
            session,
            'erpnext.stock.utils.scan_barcode',
            { search_value: term },
            { asForm: true, tags: { name: 'scan_barcode', endpoint: 'scan_barcode' } }
          ),
        ],
        { roundTrips: 1 }
      );
    } else {
      measured = measureCall(
        () => [
          postMethod(
            session,
            'frappe.desk.search.search_link',
            {
              doctype: 'Item',
              txt: term,
              query: '',
              filters: JSON.stringify({ is_sales_item: 1 }),
              page_length: 15,
            },
            { asForm: true, tags: { name: 'search_link', endpoint: 'search_link' } }
          ),
        ],
        { roundTrips: 1 }
      );
    }
  } else {
    const query =
      `khitmee_ops.api.search.search_items?q=${encodeURIComponent(term)}` +
      `&warehouse=${encodeURIComponent(warehouse)}` +
      `&price_list=${encodeURIComponent(__ENV.PRICE_LIST || manifestContext.defaultPriceList)}` +
      '&limit=15';
    measured = measureCall(
      () => [getMethod(session, query, { tags: { name: 'search_items', endpoint: 'search_items' } })],
      { roundTrips: 1 }
    );
  }

  const responses = measured.value;
  let ok = allResponsesOk(responses);
  if (ok && barcodeLookup) {
    // scan_barcode must resolve the searched value back to the expected item.
    const parsed = parseJsonBody(responses[0]);
    const body = responseObject(parsed);
    const found = body.message && typeof body.message === 'object' ? body.message.item_code : '';
    if (!found || (item.item_code && found !== item.item_code)) {
      ok = false;
    }
  }
  recordOperation(measured, searchDuration, searchServerDuration);
  recordLogicalOutcome(ok, responses, exact ? 'stock lookup' : 'search');
  if (!ok) {
    logK6Failure({ op: exact ? 'stock_lookup' : 'search', responses, warehouse });
  }
  return { ok, item, term, responses, durationMs: measured.endToEndDurationMs };
}

const ROW_MERGE_DENYLIST = {
  doctype: true,
  name: true,
  parent: true,
  parenttype: true,
  parentfield: true,
  idx: true,
  docstatus: true,
  __islocal: true,
};

function mergeRowDetails(row, details) {
  Object.keys(details).forEach((key) => {
    if (!ROW_MERGE_DENYLIST[key] && details[key] !== undefined && details[key] !== null) {
      row[key] = details[key];
    }
  });
}

function executeStockFlow(session, flow) {
  const responses = [];
  const endpoint = __ENV.RUN_DOC_METHOD_ENDPOINT || 'run_doc_method';
  const plannedItems = (flow.doc.items || []).slice();
  let currentDoc = Object.assign({}, flow.doc, { items: [] });
  let failure = '';
  flow.preCalls.forEach((call) => {
    if (failure) {
      return;
    }
    const stepItems = (currentDoc.items || []).slice(0, call.index).concat([plannedItems[call.index]]);
    const stepDoc = Object.assign({}, currentDoc, { items: stepItems });
    const response = postMethod(
      session,
      endpoint,
      {
        docs: JSON.stringify(stepDoc),
        method: call.method,
        args: JSON.stringify(call.args),
      },
      {
        asForm: true,
        tags: { name: 'run_doc_method', endpoint: 'run_doc_method' },
      }
    );
    responses.push(response);
    if (!responseOk(response)) {
      failure = `row prep call ${call.method} failed for item row ${call.index + 1} (HTTP ${response.status})`;
      return;
    }
    const body = responseObject(parseJsonBody(response));
    const returned = body.docs && body.docs.length ? body.docs[0] : null;
    if (returned && typeof returned === 'object') {
      // Carry the server-evolved document forward into the next row prep call.
      currentDoc = returned;
    }
    if (body.message && typeof body.message === 'object' && !Array.isArray(body.message)) {
      // Merge row-level details (e.g. Stock Entry get_item_details) into the target row.
      const row = (currentDoc.items || [])[call.index];
      if (row) {
        mergeRowDetails(row, body.message);
      }
    }
  });
  if (failure) {
    return responses;
  }
  if (
    currentDoc.doctype === 'Sales Invoice' &&
    currentDoc.is_pos &&
    Array.isArray(currentDoc.payments) &&
    currentDoc.payments.length
  ) {
    const total = Number(
      currentDoc.rounded_total ||
        currentDoc.grand_total ||
        (currentDoc.items || []).reduce(
          (sum, r) => sum + Number(r.amount || Number(r.qty || 0) * Number(r.rate || 0)),
          0
        )
    );
    currentDoc.payments[0].amount = total;
    currentDoc.paid_amount = total;
  }
  flow.savedocs.doc = JSON.stringify(currentDoc);
  responses.push(
    postMethod(session, 'frappe.desk.form.save.savedocs', flow.savedocs, {
      asForm: true,
      timeout: '120s',
      tags: { name: 'savedocs_submit', endpoint: 'savedocs_submit' },
    })
  );
  return responses;
}

function submitMeasured(session, voucherType, callable, roundTrips) {
  return measureCall(callable, { roundTrips });
}

export function submitVoucher(session, warehouse, voucherType, options = {}) {
  const mode = currentMode();
  const type = voucherType === 'transfer' ? 'transfer' : 'sale';
  let measured;
  let voucherId = null;
  let skuQtys = [];
  let targetWarehouse = null;

  if (type === 'sale') {
    if (mode === 'stock') {
      const flow = buildStockSaleFlow({ warehouse, contention: options.contention });
      voucherId = flow.doc.name;
      skuQtys = skuQtysFromItems(flow.doc.items);
      measured = submitMeasured(session, type, () => executeStockFlow(session, flow), flow.roundTrips);
    } else {
      const payload = buildKhitmeeSalePayload({ warehouse, contention: options.contention });
      voucherId = payload.client_voucher_id;
      skuQtys = skuQtysFromItems(payload.items);
      measured = submitMeasured(
        session,
        type,
        () => [
          postMethod(session, 'khitmee_ops.api.vouchers.submit_sale', payload, {
            timeout: '120s',
            tags: { name: 'submit_sale', endpoint: 'submit_sale' },
          }),
        ],
        1
      );
    }
  } else {
    targetWarehouse = options.targetWarehouse || chooseTransferTarget(warehouse);
    if (mode === 'stock') {
      const flow = buildStockTransferFlow({
        sourceWarehouse: warehouse,
        targetWarehouse,
        contention: options.contention,
      });
      voucherId = flow.doc.name;
      skuQtys = skuQtysFromItems(flow.doc.items);
      measured = submitMeasured(session, type, () => executeStockFlow(session, flow), flow.roundTrips);
    } else {
      const payload = buildKhitmeeTransferPayload({
        sourceWarehouse: warehouse,
        targetWarehouse,
        transferMode: options.transferMode,
        contention: options.contention,
      });
      voucherId = payload.client_voucher_id;
      skuQtys = skuQtysFromItems(payload.items);
      measured = submitMeasured(
        session,
        type,
        () => [
          postMethod(session, 'khitmee_ops.api.vouchers.submit_transfer', payload, {
            timeout: '120s',
            tags: { name: 'submit_transfer', endpoint: 'submit_transfer' },
          }),
        ],
        1
      );
    }
  }

  const responses = measured.value;
  const ok = allResponsesOk(responses);
  recordOperation(measured, submitDuration, submitServerDuration);
  recordLogicalOutcome(ok, responses, `${type} submit`);
  if (!ok) {
    logK6Failure({
      op: `${type}_submit`,
      responses,
      warehouse,
      targetWarehouse,
      voucherId,
      skuQtys,
    });
  }
  if (ok) {
    if (type === 'sale') {
      saleVouchers.add(1);
    } else {
      transferVouchers.add(1);
    }
  }
  return { ok, type, responses, durationMs: measured.endToEndDurationMs };
}

function isoDateDaysAgo(days) {
  const date = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
  return date.toISOString().slice(0, 10);
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

export function reportFilters(reportName, warehouse) {
  const window = manifestContext.reportWindow || {};
  const filters = {
    company: __ENV.COMPANY || manifestContext.company,
    from_date: __ENV.REPORT_FROM_DATE || window.from_date || isoDateDaysAgo(Number(__ENV.REPORT_LOOKBACK_DAYS || 30)),
    to_date: __ENV.REPORT_TO_DATE || window.to_date || todayIso(),
    warehouse,
  };
  if (reportName !== 'Stock Balance') {
    filters.group_by = __ENV.REPORT_GROUP_BY || 'Invoice';
  }
  return filters;
}

export function runManagerReport(session, warehouse, options = {}) {
  const mode = currentMode();
  const grossProfitName = mode === 'stock' ? 'Gross Profit' : 'Fast Gross Profit';
  const reportName = options.reportName || (Math.random() < 0.5 ? 'Stock Balance' : grossProfitName);
  const payload = {
    report_name: reportName,
    filters: JSON.stringify(reportFilters(reportName, warehouse)),
    // Force synchronous execution at the top-level query_report.run parameter.
    ignore_prepared_report: 1,
  };
  const measured = measureCall(
    () => [
      postMethod(session, 'frappe.desk.query_report.run', payload, {
        asForm: true,
        timeout: '180s',
        tags: { name: 'query_report', endpoint: 'query_report', report: reportName },
      }),
    ],
    { roundTrips: 1 }
  );
  const responses = measured.value;
  // ignore_prepared_report must yield a synchronous result set, never an async enqueue.
  const parsed = parseJsonBody(responses[0]);
  const body = parsed.ok ? responseObject(parsed) : null;
  const message = body ? body.message : null;
  const ok =
    allResponsesOk(responses) &&
    Boolean(message && typeof message === 'object') &&
    Array.isArray(message.result) &&
    !message.error &&
    !message.prepared_report &&
    !message.prepare_report;
  recordLogicalOutcome(ok, responses, `${reportName} report`);
  if (!ok) {
    logK6Failure({ op: 'manager_report', responses, warehouse, reportName });
  }
  if (ok) {
    recordOperation(measured, reportDuration, reportServerDuration, { report: reportName });
  }
  return { ok, reportName, responses, durationMs: measured.endToEndDurationMs };
}

export function performShopIteration(session, warehouse, options = {}) {
  performSearch(session, warehouse, { contention: options.contention });
  const stockLookupRatio = Number(__ENV.STOCK_LOOKUP_RATIO || 0.25);
  if (Math.random() < stockLookupRatio) {
    performSearch(session, warehouse, { exact: true, contention: options.contention });
  }

  const role = warehouseRole(warehouse);
  let voucherType;
  if (role === 'distribution') {
    voucherType = Math.random() < Number(__ENV.DIST_TRANSFER_RATIO || 0.6) ? 'transfer' : 'sale';
  } else {
    voucherType = Math.random() < Number(__ENV.RETAIL_SALE_RATIO || 0.85) ? 'sale' : 'transfer';
  }
  return submitVoucher(session, warehouse, voucherType, options);
}

export function warehouseForVu(vuIndex) {
  const sequence = shopWarehouseSequence();
  return sequence[(Math.max(1, vuIndex) - 1) % sequence.length];
}

export function randomWarehouse() {
  return randomItem(shopWarehouseSequence());
}

export function noteInternalError(err) {
  errors.add(1);
  errorRate.add(1);
  throw err;
}
