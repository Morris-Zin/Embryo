import http from 'k6/http';
import execution from 'k6/execution';
import { sleep } from 'k6';
import crypto from 'k6/crypto';
import { getMethod, getResource, getResourceRequest, postMethod, postMethodRequest } from './auth.js';
import { sleepForSimulatedRtt } from './latency.js';
import {
  errorRate,
  errors,
  reportDuration,
  reportServerDuration,
  searchDuration,
  searchServerDuration,
  seasonalDetailFallbacks,
  seasonalDetailFirstRowsDuration,
  seasonalExportBytes,
  seasonalExportDuration,
  seasonalExportRows,
  seasonalVoucherCompleted,
  seasonalVoucherLinesCompleted,
  seasonalVoucherLostAck,
  seasonalVoucherRecoveryReads,
  seasonalVoucherRejected,
  seasonalVoucherRetried,
  seasonalVoucherStarted,
  seasonalVoucherUnknown,
  submitDuration,
  submitServerDuration,
} from './metrics.js';
import { manifestContext } from './payloads.js';
import { parseJsonBody, responseOk } from './workflow.js';
import {
  basketHash,
  deterministicLines,
  hashHex,
  contentionSaleSequence,
  contentionTransferSequence,
  makeClientVoucherId,
  retryWitness,
  stableHash32,
} from './seasonal_plan.js';
import {
  ambiguousSubmitResponse,
  isPurchaseVoucherType,
  mayResubmitAfterSubmitResponse,
  purchaseLookupSpec,
  validateRecoveredPurchaseDocument,
} from './purchase_recovery.js';

const EVENT_PREFIX = 'KHITMEE_SEASONAL_EVENT';
const SALE_ENDPOINT = 'khitmee_ops.api.vouchers.submit_sale';
const TRANSFER_ENDPOINT = 'khitmee_ops.api.vouchers.submit_transfer';
const SAVEDOCS_ENDPOINT = 'frappe.desk.form.save.savedocs';
const CONTENTION_SHOP_COUNT = 6;
const EXPECTED_DOCTYPES = {
  sale: 'Sales Invoice',
  transfer: 'Stock Entry',
  purchase_receipt: 'Purchase Receipt',
  purchase_invoice: 'Purchase Invoice',
};

export function logSeasonalEvent(payload) {
  const event = Object.assign({ at: new Date().toISOString() }, payload);
  try {
    console.log(`${EVENT_PREFIX} ${JSON.stringify(event)}`);
  } catch (err) {
    console.log(`${EVENT_PREFIX} ${JSON.stringify({ event: 'event_encode_error', error: String(err) })}`);
  }
}

function messageBody(response) {
  const parsed = parseJsonBody(response);
  if (!parsed.ok || !parsed.data || typeof parsed.data !== 'object') {
    return {};
  }
  const body = parsed.data;
  if (body.message && typeof body.message === 'object' && !Array.isArray(body.message)) {
    return body.message;
  }
  return body;
}

function canonicalNumber(value) {
  if (!Number.isFinite(value)) {
    throw new Error('non-finite numbers cannot be canonically hashed');
  }
  if (Object.is(value, -0)) {
    return '0';
  }
  if (Number.isInteger(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER) {
    return String(value);
  }
  return value.toFixed(15).replace(/0+$/, '').replace(/\.$/, '');
}

function canonicalJson(value) {
  if (typeof value === 'number') {
    return canonicalNumber(value);
  }
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return '[' + value.map((entry) => canonicalJson(entry)).join(',') + ']';
  }
  return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
}

function canonicalHash(value) {
  return crypto.sha256(canonicalJson(value), 'hex');
}

function responseBodyText(response) {
  if (!response || response.body === undefined || response.body === null) {
    return '';
  }
  return String(response.body);
}

function metricTags(work) {
  return {
    phase: String(work.phase || 'unknown'),
    segment: String(work.segment || 'unknown'),
    voucher_type: String(work.type || 'unknown'),
    line_count: String(work.lineCount || 0),
  };
}

function managerTags(phase, extra = {}) {
  return Object.assign({ phase: String(phase || 'unknown') }, extra);
}

function choose(values, index) {
  if (!values.length) {
    throw new Error('Cannot choose from an empty warehouse/party list');
  }
  return values[((index % values.length) + values.length) % values.length];
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function round2(value) {
  return Math.round(Number(value) * 100) / 100;
}

function purchaseRate(line, seedKey) {
  const selling = Number(line.rate);
  const base = Number.isFinite(selling) && selling > 0 ? selling : 10 + (stableHash32(`${seedKey}:rate:${line.item_code}`) % 90);
  return Math.max(0.01, round2(base * 0.7));
}

function childDocumentRows(doctype, childDoctype, parentName, lines, warehouse, seedKey, extraRowFields = {}) {
  return lines.map((line, index) => Object.assign(
    {
      doctype: childDoctype,
      __islocal: 1,
      docstatus: 0,
      name: `${parentName}-item-${index + 1}`,
      parent: parentName,
      parenttype: doctype,
      parentfield: 'items',
      idx: index + 1,
      item_code: line.item_code,
      qty: line.qty,
      uom: line.uom,
      warehouse,
      rate: purchaseRate(line, seedKey),
    },
    extraRowFields(line, index)
  ));
}

function purchaseReceiptDocument(built) {
  const parentName = `new-purchase-receipt-${hashHex(built.clientVoucherId).slice(0, 12)}`;
  return {
    doctype: 'Purchase Receipt',
    __islocal: 1,
    docstatus: 0,
    name: parentName,
    supplier: built.supplier,
    company: manifestContext.company,
    posting_date: todayIso(),
    set_posting_time: 1,
    set_warehouse: built.warehouse,
    currency: manifestContext.defaultCurrency,
    conversion_rate: 1,
    remarks: `Khitmee seasonal client_voucher_id=${built.clientVoucherId}`,
    items: childDocumentRows(
      'Purchase Receipt',
      'Purchase Receipt Item',
      parentName,
      built.lines,
      built.warehouse,
      built.clientVoucherId,
      () => ({})
    ),
  };
}

function purchaseInvoiceDocument(built, receiptDocument) {
  if (!receiptDocument || !receiptDocument.name || !Array.isArray(receiptDocument.items)) {
    throw new Error('linked purchase invoice requires the submitted Purchase Receipt document');
  }
  if (receiptDocument.items.length !== built.lines.length) {
    throw new Error(`linked Purchase Receipt returned ${receiptDocument.items.length} rows, expected ${built.lines.length}`);
  }
  const parentName = `new-purchase-invoice-${hashHex(built.clientVoucherId).slice(0, 12)}`;
  return {
    doctype: 'Purchase Invoice',
    __islocal: 1,
    docstatus: 0,
    name: parentName,
    supplier: built.supplier,
    company: manifestContext.company,
    posting_date: todayIso(),
    due_date: todayIso(),
    bill_date: todayIso(),
    bill_no: built.clientVoucherId,
    set_posting_time: 1,
    update_stock: 0,
    currency: manifestContext.defaultCurrency,
    conversion_rate: 1,
    remarks: `Khitmee seasonal client_voucher_id=${built.clientVoucherId}`,
    items: childDocumentRows(
      'Purchase Invoice',
      'Purchase Invoice Item',
      parentName,
      built.lines,
      built.warehouse,
      built.clientVoucherId,
      (_line, index) => ({
        purchase_receipt: receiptDocument.name,
        pr_detail: receiptDocument.items[index].name,
      })
    ),
  };
}

export function buildSeasonalWorkPayload(plan, work, overrides = {}) {
  const settings = plan.settings;
  const effective = Object.assign({}, work, overrides);
  if (effective.type === 'purchase_cycle') {
    throw new Error('purchase_cycle is a workflow schedule entry; build its purchase_receipt/purchase_invoice children');
  }
  const warehouses = plan.warehouses;
  const allWarehouses = warehouses.all;
  const distribution = warehouses.distribution;
  const retail = warehouses.retail;
  const isPurchase = effective.type === 'purchase_receipt' || effective.type === 'purchase_invoice';
  const pairScope = isPurchase ? 'purchase_pair' : effective.mixKey;
  const basketSeed = [
    settings.runId,
    settings.seed,
    effective.segment,
    pairScope,
    effective.minute,
    effective.sequence,
    effective.lineCount,
  ].join(':');

  let warehouse = overrides.warehouse || '';
  let targetWarehouse = overrides.targetWarehouse || '';
  if (effective.type === 'sale') {
    warehouse = warehouse ||
      (effective.lineCount === 200
        ? choose(distribution, effective.minute + effective.sequence)
        : choose(allWarehouses, effective.minute * 6 + effective.sequence));
  } else if (effective.type === 'transfer') {
    warehouse = warehouse || overrides.sourceWarehouse || choose(distribution, effective.minute + effective.sequence);
    targetWarehouse = targetWarehouse || choose(retail, effective.minute + effective.sequence + 1);
  } else if (isPurchase) {
    warehouse = warehouse || choose(distribution, effective.minute + effective.sequence);
  } else {
    throw new Error(`Unsupported seasonal voucher type '${effective.type}'`);
  }

  const lines = deterministicLines(manifestContext, warehouse, effective.lineCount, basketSeed, {
    distribution: effective.type !== 'sale' || effective.lineCount === 200 || distribution.includes(warehouse),
    includeRates: effective.type !== 'transfer',
    requiredItemCode: overrides.requiredItemCode,
  });
  const clientVoucherId = makeClientVoucherId(settings, effective);
  const supplier = choose(manifestContext.suppliers, effective.minute + effective.sequence);
  const built = {
    work: effective,
    clientVoucherId,
    warehouse,
    targetWarehouse,
    supplier: isPurchase ? supplier : '',
    lines,
    lineHash: basketHash(lines),
    payload: null,
    document: null,
  };

  if (effective.type === 'sale') {
    built.payload = {
      client_voucher_id: clientVoucherId,
      warehouse,
      customer: manifestContext.defaultCustomer,
      price_list: manifestContext.defaultPriceList,
      items: lines,
      payment_mode: manifestContext.paymentMode || 'Cash',
      printer_type: 'voucher',
    };
  } else if (effective.type === 'transfer') {
    built.payload = {
      client_voucher_id: clientVoucherId,
      source_warehouse: warehouse,
      target_warehouse: targetWarehouse,
      transfer_mode: 'direct',
      items: lines,
      printer_type: 'voucher',
    };
  } else if (effective.type === 'purchase_receipt') {
    built.document = purchaseReceiptDocument(built);
  } else if (effective.type === 'purchase_invoice') {
    built.document = overrides.receiptDocument && overrides.receiptDocument.name
      ? purchaseInvoiceDocument(built, overrides.receiptDocument)
      : null;
  }
  return built;
}

function savedocsDocument(response, expectedDoctype) {
  const parsed = parseJsonBody(response);
  if (!parsed.ok || !parsed.data || typeof parsed.data !== 'object') {
    return null;
  }
  const body = parsed.data;
  const docs = Array.isArray(body.docs)
    ? body.docs
    : body.message && Array.isArray(body.message.docs)
      ? body.message.docs
      : [];
  return docs.find((doc) => doc && doc.doctype === expectedDoctype) || null;
}


function normalizedBasketHash(lines) {
  return basketHash((lines || [])
    .map((line) => ({
      item_code: line.item_code,
      qty: Number(line.qty),
      uom: line.uom,
    }))
    .sort((a, b) => String(a.item_code).localeCompare(String(b.item_code))));
}

function validateVoucherResponse(response, built) {
  if (!responseOk(response)) {
    return { ok: false, reason: `HTTP ${response ? response.status : 'no response'} or Frappe error` };
  }
  const expectedDoctype = EXPECTED_DOCTYPES[built.work.type];
  const isPurchase = built.work.type === 'purchase_receipt' || built.work.type === 'purchase_invoice';
  if (isPurchase) {
    if (!built.document) {
      return { ok: false, reason: `${expectedDoctype} document was not built` };
    }
    const doc = savedocsDocument(response, expectedDoctype);
    if (!doc) {
      return { ok: false, reason: `savedocs response did not include the submitted ${expectedDoctype}` };
    }
    if (Number(doc.docstatus) !== 1) {
      return { ok: false, reason: `${expectedDoctype} returned docstatus ${doc.docstatus}, expected 1` };
    }
    if (!Array.isArray(doc.items) || doc.items.length !== built.work.lineCount) {
      return {
        ok: false,
        reason: `${expectedDoctype} returned ${doc.items ? doc.items.length : 0} rows, expected ${built.work.lineCount}`,
      };
    }
    if (normalizedBasketHash(doc.items) !== built.lineHash) {
      return { ok: false, reason: `${expectedDoctype} acknowledgement does not match the seeded basket` };
    }
    if (built.work.type === 'purchase_invoice') {
      for (let index = 0; index < doc.items.length; index += 1) {
        const expected = built.document.items[index] || {};
        if (doc.items[index].purchase_receipt !== expected.purchase_receipt || doc.items[index].pr_detail !== expected.pr_detail) {
          return { ok: false, reason: 'Purchase Invoice acknowledgement is not linked to the confirmed Purchase Receipt rows' };
        }
      }
    }
    return { ok: true, voucherNo: doc.name, document: doc, message: doc };
  }

  const message = messageBody(response);
  const actualDoctype = message.doctype || message.reference_doctype || message.voucher_doctype || '';
  if (actualDoctype && actualDoctype !== expectedDoctype) {
    return { ok: false, reason: `expected ${expectedDoctype}, got ${actualDoctype}` };
  }
  const voucherNo = message.voucher_no || message.name || message.docname || '';
  if (!voucherNo) {
    return { ok: false, reason: 'confirmation did not include voucher_no/name' };
  }
  if (message.client_voucher_id && message.client_voucher_id !== built.clientVoucherId) {
    return { ok: false, reason: 'confirmation client_voucher_id does not match the submitted identity' };
  }
  if (!Array.isArray(message.items) || message.items.length !== built.work.lineCount) {
    return {
      ok: false,
      reason: `confirmation returned ${Array.isArray(message.items) ? message.items.length : 0} item rows, expected ${built.work.lineCount}`,
    };
  }
  if (normalizedBasketHash(message.items) !== built.lineHash) {
    return { ok: false, reason: 'confirmation does not match the seeded basket' };
  }
  return { ok: true, voucherNo, message };
}

function retryableResponse(type, response) {
  return mayResubmitAfterSubmitResponse(type, response);
}

function endpointFor(type) {
  if (type === 'sale') {
    return SALE_ENDPOINT;
  }
  if (type === 'transfer') {
    return TRANSFER_ENDPOINT;
  }
  throw new Error(`Unsupported direct seasonal endpoint type '${type}'`);
}

function sendVoucherAttempt(session, built) {
  sleepForSimulatedRtt(1);
  const serverStart = Date.now();
  let response;
  const tags = {
    name: `seasonal_${built.work.type}`,
    endpoint: `seasonal_${built.work.type}`,
    voucher_type: built.work.type,
    phase: built.work.phase,
  };
  const isPurchase = built.work.type === 'purchase_receipt' || built.work.type === 'purchase_invoice';
  if (isPurchase) {
    if (!built.document) {
      throw new Error(`${built.work.type} cannot be submitted before its document is built`);
    }
    response = postMethod(
      session,
      SAVEDOCS_ENDPOINT,
      { doc: JSON.stringify(built.document), action: 'Submit' },
      { asForm: true, timeout: '120s', tags }
    );
  } else {
    response = postMethod(session, endpointFor(built.work.type), built.payload, { timeout: '120s', tags });
  }
  return { response, serverDurationMs: Math.max(0, Date.now() - serverStart) };
}

function voucherRequest(session, built) {
  const tags = {
    name: `seasonal_${built.work.type}`,
    endpoint: `seasonal_${built.work.type}`,
    voucher_type: built.work.type,
    phase: built.work.phase,
  };
  if (isPurchaseVoucherType(built.work.type)) {
    if (!built.document) {
      throw new Error(`${built.work.type} cannot be submitted before its document is built`);
    }
    return postMethodRequest(
      session,
      SAVEDOCS_ENDPOINT,
      { doc: JSON.stringify(built.document), action: 'Submit' },
      { asForm: true, timeout: '120s', tags }
    );
  }
  return postMethodRequest(session, endpointFor(built.work.type), built.payload, { timeout: '120s', tags });
}

function batchEntry(request) {
  return {
    method: request.method,
    url: request.url,
    body: request.body,
    params: request.params,
  };
}

function responseDurationMs(response, fallbackMs) {
  const duration = response && response.timings && Number(response.timings.duration);
  return Number.isFinite(duration) && duration >= 0 ? duration : fallbackMs;
}

function logRetryOverlap(built, mode) {
  const work = built.work;
  logSeasonalEvent({
    event: 'voucher_retry_overlap',
    phase: work.phase,
    segment: work.segment,
    overload_step: work.overloadStep || 0,
    type: work.type,
    mix_key: work.mixKey,
    line_count: work.lineCount,
    minute: work.minute,
    sequence: work.sequence,
    client_voucher_id: built.clientVoucherId,
    mode,
    concurrent_attempts: 2,
    first_inflight: true,
  });
}

function emitVoucherStart(built) {
  const work = built.work;
  const purchase = work.type === 'purchase_receipt' || work.type === 'purchase_invoice';
  const replayInput = {
    company: manifestContext.company,
    currency: manifestContext.defaultCurrency,
    items: (purchase && built.document ? built.document.items : built.lines).map((line) => {
      const item = { item_code: line.item_code, qty: line.qty, uom: line.uom };
      if (purchase && built.document) item.rate = line.rate;
      return item;
    }),
  };
  if (work.type === 'sale') {
    Object.assign(replayInput, {
      customer: built.payload.customer, price_list: built.payload.price_list, payment_mode: built.payload.payment_mode,
    });
  }
  if (purchase && built.document) replayInput.posting_date = built.document.posting_date;
  seasonalVoucherStarted.add(1, metricTags(work));
  logSeasonalEvent({
    event: 'voucher_start',
    phase: work.phase,
    segment: work.segment,
    overload_step: work.overloadStep || 0,
    type: work.type,
    mix_key: work.mixKey,
    line_count: work.lineCount,
    minute: work.minute,
    sequence: work.sequence,
    client_voucher_id: built.clientVoucherId,
    replay_input: replayInput,
    actor: built.actor || 'cashier',
    actor_user: built.actorUser || '',
    warehouse: built.warehouse,
    target_warehouse: built.targetWarehouse,
    supplier: built.supplier,
    basket_hash: built.lineHash,
    distinct_items: new Set(built.lines.map((line) => line.item_code)).size,
    contention: Boolean(work.contention),
    shared_contention_item: work.sharedContentionItem || '',
  });
}


function logVoucherBuild(event, work, extra = {}) {
  logSeasonalEvent(Object.assign({
    event,
    phase: work.phase,
    segment: work.segment,
    overload_step: work.overloadStep || 0,
    type: work.type,
    mix_key: work.mixKey,
    line_count: work.lineCount,
    minute: work.minute,
    sequence: work.sequence,
    contention: Boolean(work.contention),
  }, extra));
}

function finishVoucher(built, outcome) {
  const work = built.work;
  const tags = metricTags(work);
  const retried = outcome.retried === undefined ? outcome.attempts > 1 : Boolean(outcome.retried);
  const resubmitted = outcome.resubmitted === undefined ? outcome.attempts > 1 : Boolean(outcome.resubmitted);
  const common = {
    phase: work.phase,
    segment: work.segment,
    overload_step: work.overloadStep || 0,
    type: work.type,
    mix_key: work.mixKey,
    line_count: work.lineCount,
    minute: work.minute,
    sequence: work.sequence,
    client_voucher_id: built.clientVoucherId,
    actor: built.actor || 'cashier',
    actor_user: built.actorUser || '',
    warehouse: built.warehouse,
    target_warehouse: built.targetWarehouse,
    supplier: built.supplier,
    basket_hash: built.lineHash,
    distinct_items: new Set(built.lines.map((line) => line.item_code)).size,
    contention: Boolean(work.contention),
    shared_contention_item: work.sharedContentionItem || '',
    attempts: outcome.attempts,
    retried,
    resubmitted,
    recovery_attempted: Boolean(outcome.recoveryAttempted),
    recovery_reads: outcome.recoveryReads || 0,
    reconciled: Boolean(outcome.reconciled),
    lost_ack: outcome.lostAck,
    first_committed_voucher: outcome.firstCommittedVoucher || '',
    retry_overlap: outcome.retryOverlap === undefined ? Boolean(built.retryOverlapEvidence) : Boolean(outcome.retryOverlap),
    retry_overlap_mode: outcome.retryOverlapMode || (built.retryOverlapEvidence || {}).mode || '',
    concurrent_attempts: outcome.concurrentAttempts || (built.retryOverlapEvidence || {}).concurrentAttempts || 0,
    duration_ms: outcome.durationMs,
    server_duration_ms: outcome.serverDurationMs,
  };
  if (outcome.ok) {
    seasonalVoucherCompleted.add(1, tags);
    seasonalVoucherLinesCompleted.add(work.lineCount, tags);
    if (retried) {
      seasonalVoucherRetried.add(1, tags);
    }
    submitDuration.add(outcome.durationMs, tags);
    submitServerDuration.add(outcome.serverDurationMs, tags);
    logSeasonalEvent(Object.assign(common, {
      event: 'voucher_complete',
      voucher_no: outcome.voucherNo,
      response_doctype: EXPECTED_DOCTYPES[work.type],
      retry_confirmed: outcome.retryConfirmed === undefined ? retried : Boolean(outcome.retryConfirmed),
    }));
    return { ok: true, voucherNo: outcome.voucherNo, document: outcome.document || null };
  }
  errors.add(1);
  errorRate.add(1);
  if (outcome.unknown) {
    seasonalVoucherUnknown.add(1, tags);
    logSeasonalEvent(Object.assign(common, {
      event: 'voucher_unknown',
      status: outcome.status,
      reason: outcome.reason,
    }));
    return { ok: false, unknown: true, reason: outcome.reason };
  }
  seasonalVoucherRejected.add(1, tags);
  logSeasonalEvent(Object.assign(common, {
    event: 'voucher_reject',
    status: outcome.status,
    reason: outcome.reason,
  }));
  return { ok: false, reason: outcome.reason };
}

function resourceBody(response) {
  const parsed = parseJsonBody(response);
  if (!parsed.ok || !parsed.data || typeof parsed.data !== 'object') {
    return null;
  }
  return parsed.data;
}

function resourceRows(response) {
  const body = resourceBody(response);
  const rows = body && (Array.isArray(body.data) ? body.data : body.message);
  return Array.isArray(rows) ? rows : null;
}

function resourceDocument(response) {
  const body = resourceBody(response);
  const doc = body && (body.data && typeof body.data === 'object' && !Array.isArray(body.data) ? body.data : body.message);
  return doc && typeof doc === 'object' && !Array.isArray(doc) ? doc : null;
}

function recoveryTags(work) {
  return {
    name: 'seasonal_purchase_recovery',
    endpoint: 'seasonal_purchase_recovery',
    voucher_type: work.type,
    phase: work.phase,
  };
}

function recoveryListOptions(spec, tags) {
  return {
    query: {
      fields: JSON.stringify(spec.fields),
      filters: JSON.stringify(spec.filters),
      limit_page_length: String(spec.limitPageLength),
    },
    timeout: '30s',
    tags,
  };
}

function recoverPurchaseDocument(session, settings, built, firstListResponse = null) {
  const spec = purchaseLookupSpec(built);
  const polls = Math.max(1, settings.purchaseRecoveryPolls || 1);
  const tags = recoveryTags(built.work);
  let reads = 0;
  let lastReason = 'permission-aware recovery lookup did not run';
  for (let poll = 0; poll < polls; poll += 1) {
    const options = recoveryListOptions(spec, tags);
    const listResponse = poll === 0 && firstListResponse
      ? firstListResponse
      : getResource(session, spec.doctype, options);
    reads += 1;
    if (!responseOk(listResponse)) {
      lastReason = `permission-aware lookup unreadable: HTTP ${listResponse ? listResponse.status : 'no response'}`;
    } else {
      const rows = resourceRows(listResponse);
      if (!rows) {
        lastReason = 'permission-aware lookup unreadable: malformed resource list';
      } else if (rows.length === 0) {
        lastReason = 'permission-aware lookup missing: no submitted document carries the original client identity';
      } else if (rows.length > 1) {
        return {
          ok: false,
          unknown: true,
          reason: `permission-aware lookup ambiguous: ${rows.length} submitted documents carry the original client identity`,
          reads,
        };
      } else if (!rows[0] || !rows[0].name) {
        return { ok: false, unknown: true, reason: 'permission-aware lookup ambiguous: candidate row has no document name', reads };
      } else {
        const documentResponse = getResource(session, spec.doctype, {
          name: rows[0].name,
          timeout: '30s',
          tags,
        });
        reads += 1;
        if (!responseOk(documentResponse)) {
          lastReason = `permission-aware document read unreadable: HTTP ${documentResponse ? documentResponse.status : 'no response'}`;
        } else {
          const doc = resourceDocument(documentResponse);
          if (!doc) {
            return { ok: false, unknown: true, reason: 'permission-aware document read unreadable: malformed resource document', reads };
          }
          const validation = validateRecoveredPurchaseDocument(doc, built);
          if (validation.ok) {
            return {
              ok: true,
              voucherNo: validation.voucherNo,
              document: validation.document,
              reads,
            };
          }
          return {
            ok: false,
            unknown: true,
            reason: `permission-aware lookup ambiguous: candidate is not identical to the original purchase (${validation.reason})`,
            reads,
          };
        }
      }
    }
    if (poll + 1 < polls && settings.purchaseRecoveryPollIntervalMs > 0) {
      sleep(settings.purchaseRecoveryPollIntervalMs / 1000);
    }
  }
  return { ok: false, unknown: true, reason: lastReason, reads };
}

function resolvePurchaseVoucher(session, settings, built, firstAttempt, uxStart, firstRecoveryListResponse = null) {
  const attempt = firstAttempt || sendVoucherAttempt(session, built);
  const attempts = 1;
  const serverDurationMs = attempt.serverDurationMs;
  const lastResponse = attempt.response;
  let lostAck = false;
  let firstCommittedVoucher = '';
  let validation = validateVoucherResponse(lastResponse, built);
  if (!lastResponse || lastResponse.status === 0) {
    lostAck = true;
    seasonalVoucherLostAck.add(1, metricTags(built.work));
  }
  if (validation.ok) {
    if (!(settings.requireRetryEvidence && retryWitness(built.work))) {
      return finishVoucher(built, {
        ok: true,
        voucherNo: validation.voucherNo,
        document: validation.document,
        attempts,
        resubmitted: false,
        lostAck,
        durationMs: Math.max(0, Date.now() - uxStart),
        serverDurationMs,
      });
    }
    // The observer uses the first response only as benchmark evidence. Recovery
    // below receives no server-assigned voucher name and must rediscover the
    // document solely from the original client identity and payload.
    firstCommittedVoucher = validation.voucherNo;
    lostAck = true;
    seasonalVoucherLostAck.add(1, metricTags(built.work));
  } else if (!ambiguousSubmitResponse(lastResponse)) {
    return finishVoucher(built, {
      ok: false,
      reason: validation.reason || 'purchase submit was rejected before an ambiguous outcome',
      status: lastResponse ? lastResponse.status : 0,
      attempts,
      resubmitted: false,
      lostAck,
      firstCommittedVoucher,
      durationMs: Math.max(0, Date.now() - uxStart),
      serverDurationMs,
    });
  }

  const recovery = recoverPurchaseDocument(session, settings, built, firstRecoveryListResponse);
  seasonalVoucherRecoveryReads.add(recovery.reads, metricTags(built.work));
  const recoveredSameVoucher = recovery.ok && (!firstCommittedVoucher || recovery.voucherNo === firstCommittedVoucher);
  if (recoveredSameVoucher) {
    return finishVoucher(built, {
      ok: true,
      voucherNo: recovery.voucherNo,
      document: recovery.document,
      attempts,
      retried: true,
      retryConfirmed: true,
      resubmitted: false,
      recoveryAttempted: true,
      recoveryReads: recovery.reads,
      reconciled: true,
      lostAck,
      firstCommittedVoucher,
      durationMs: Math.max(0, Date.now() - uxStart),
      serverDurationMs,
    });
  }
  if (recovery.ok) {
    return finishVoucher(built, {
      ok: false,
      reason: 'recovered purchase voucher does not match the first committed voucher observed by the benchmark',
      status: lastResponse ? lastResponse.status : 0,
      attempts,
      retried: true,
      resubmitted: false,
      recoveryAttempted: true,
      recoveryReads: recovery.reads,
      reconciled: true,
      lostAck,
      firstCommittedVoucher,
      durationMs: Math.max(0, Date.now() - uxStart),
      serverDurationMs,
    });
  }
  return finishVoucher(built, {
    ok: false,
    unknown: true,
    reason: recovery.reason,
    status: lastResponse ? lastResponse.status : 0,
    attempts,
    retried: true,
    resubmitted: false,
    recoveryAttempted: true,
    recoveryReads: recovery.reads,
    reconciled: false,
    lostAck,
    firstCommittedVoucher,
    durationMs: Math.max(0, Date.now() - uxStart),
    serverDurationMs,
  });
}

function resolvePurchaseVoucherInFlight(session, settings, built, uxStart) {
  const submitRequest = voucherRequest(session, built);
  const spec = purchaseLookupSpec(built);
  const listRequest = getResourceRequest(session, spec.doctype, recoveryListOptions(spec, recoveryTags(built.work)));
  logRetryOverlap(built, 'submit_plus_readonly_recovery_batch');
  built.retryOverlapEvidence = { mode: 'submit_plus_readonly_recovery_batch', concurrentAttempts: 2 };
  sleepForSimulatedRtt(1);
  const batchStart = Date.now();
  const responses = http.batch([batchEntry(submitRequest), batchEntry(listRequest)]);
  const elapsed = Math.max(0, Date.now() - batchStart);
  return resolvePurchaseVoucher(
    session,
    settings,
    built,
    { response: responses[0], serverDurationMs: responseDurationMs(responses[0], elapsed) },
    uxStart,
    responses[1]
  );
}

function resolveDirectVoucherInFlight(session, settings, built, uxStart) {
  const firstRequest = voucherRequest(session, built);
  const retryRequest = voucherRequest(session, built);
  logRetryOverlap(built, 'duplicate_idempotent_submit_batch');
  built.retryOverlapEvidence = { mode: 'duplicate_idempotent_submit_batch', concurrentAttempts: 2 };
  sleepForSimulatedRtt(1);
  const batchStart = Date.now();
  const responses = http.batch([batchEntry(firstRequest), batchEntry(retryRequest)]);
  const elapsed = Math.max(0, Date.now() - batchStart);
  let attempts = 2;
  let serverDurationMs = responseDurationMs(responses[0], elapsed) + responseDurationMs(responses[1], elapsed);
  const lostAck = true;
  seasonalVoucherLostAck.add(1, metricTags(built.work));
  const firstValidation = validateVoucherResponse(responses[0], built);
  let validation = validateVoucherResponse(responses[1], built);
  let lastResponse = responses[1];
  const firstCommittedVoucher = firstValidation.ok ? firstValidation.voucherNo : '';

  if (firstValidation.ok && validation.ok && validation.voucherNo !== firstCommittedVoucher) {
    validation = { ok: false, reason: 'in-flight retry returned a different committed voucher' };
  } else if (!validation.ok && firstValidation.ok) {
    validation = { ok: false, reason: `in-flight retry did not confirm the original voucher: ${validation.reason}` };
  } else if (!firstValidation.ok && validation.ok) {
    // The retry returned a voucher, but the first response was ambiguous. Keep
    // first_committed_voucher empty so the evaluator does not treat this as a
    // proven committed-first replay; the independent verifier still checks for
    // duplicate postings.
    validation = validation;
  }

  if (!validation.ok && attempts < settings.maxRetries && retryableResponse(built.work.type, lastResponse)) {
    sleep(settings.retryBackoffMs / 1000);
    const confirmation = sendVoucherAttempt(session, built);
    attempts += 1;
    serverDurationMs += confirmation.serverDurationMs;
    lastResponse = confirmation.response;
    validation = validateVoucherResponse(lastResponse, built);
    if (firstCommittedVoucher && validation.ok && validation.voucherNo !== firstCommittedVoucher) {
      validation = { ok: false, reason: 'post-overlap retry returned a different committed voucher' };
    }
  }

  return finishVoucher(built, {
    ok: validation.ok,
    voucherNo: validation.voucherNo || '',
    document: validation.document || null,
    reason: validation.reason || 'unknown validation failure',
    status: lastResponse ? lastResponse.status : 0,
    attempts,
    resubmitted: true,
    retried: true,
    lostAck,
    firstCommittedVoucher,
    durationMs: Math.max(0, Date.now() - uxStart),
    serverDurationMs,
  });
}

function resolveVoucherAttempts(session, settings, built, firstAttempt, uxStart) {
  const witness = settings.requireRetryEvidence && retryWitness(built.work);
  if (!firstAttempt && witness && isPurchaseVoucherType(built.work.type)) {
    return resolvePurchaseVoucherInFlight(session, settings, built, uxStart);
  }
  if (!firstAttempt && witness) {
    return resolveDirectVoucherInFlight(session, settings, built, uxStart);
  }
  if (isPurchaseVoucherType(built.work.type)) {
    return resolvePurchaseVoucher(session, settings, built, firstAttempt, uxStart);
  }
  let attempts = 0;
  let serverDurationMs = 0;
  let lostAck = false;
  let lastResponse = null;
  let firstCommittedVoucher = '';
  let validation = { ok: false, reason: 'not attempted' };
  while (attempts < settings.maxRetries) {
    const attempt = attempts === 0 && firstAttempt ? firstAttempt : sendVoucherAttempt(session, built);
    attempts += 1;
    serverDurationMs += attempt.serverDurationMs;
    lastResponse = attempt.response;
    if (!lastResponse || lastResponse.status === 0) {
      lostAck = true;
      seasonalVoucherLostAck.add(1, metricTags(built.work));
    }
    validation = validateVoucherResponse(lastResponse, built);
    // The observer sees the committed response. The simulated cashier drops it
    // and retries with the original client ID and payload.
    if (attempts === 1 && validation.ok && settings.requireRetryEvidence && retryWitness(built.work)) {
      firstCommittedVoucher = validation.voucherNo;
      lostAck = true;
      seasonalVoucherLostAck.add(1, metricTags(built.work));
      validation = { ok: false, reason: 'first committed confirmation withheld by benchmark' };
      sleep(settings.retryBackoffMs / 1000);
      continue;
    }
    if (firstCommittedVoucher && validation.ok && validation.voucherNo !== firstCommittedVoucher) {
      validation = { ok: false, reason: 'retry returned a different committed voucher' };
      break;
    }
    if (validation.ok) {
      break;
    }
    if (attempts >= settings.maxRetries || !retryableResponse(built.work.type, lastResponse)) {
      break;
    }
    sleep(settings.retryBackoffMs / 1000);
  }
  return finishVoucher(built, {
    ok: validation.ok,
    voucherNo: validation.voucherNo || '',
    document: validation.document || null,
    reason: validation.reason || 'unknown validation failure',
    status: lastResponse ? lastResponse.status : 0,
    attempts,
    resubmitted: attempts > 1,
    lostAck,
    firstCommittedVoucher,
    durationMs: Math.max(0, Date.now() - uxStart),
    serverDurationMs,
  });
}

function purchaseChildWork(work, type) {
  return Object.assign({}, work, {
    mixKey: type,
    type,
    lineCount: 50,
    contention: false,
  });
}

function rejectDependency(built, reason) {
  emitVoucherStart(built);
  return finishVoucher(built, {
    ok: false,
    reason,
    status: null,
    attempts: 0,
    lostAck: false,
    durationMs: 0,
    serverDurationMs: 0,
  });
}

function tagCashierActor(built, session) {
  built.actor = 'cashier';
  built.actorUser = session.user;
  return built;
}

export function submitPurchaseCycle(session, plan, work) {
  const settings = plan.settings;
  const receiptWork = purchaseChildWork(work, 'purchase_receipt');
  const invoiceWork = purchaseChildWork(work, 'purchase_invoice');
  logVoucherBuild('voucher_build_begin', receiptWork);
  const receiptBuildStart = Date.now();
  const builtReceipt = tagCashierActor(buildSeasonalWorkPayload(plan, receiptWork), session);
  logVoucherBuild('voucher_build_end', receiptWork, {
    client_voucher_id: builtReceipt.clientVoucherId,
    build_ms: Math.max(0, Date.now() - receiptBuildStart),
  });
  emitVoucherStart(builtReceipt);
  const receipt = resolveVoucherAttempts(session, settings, builtReceipt, null, Date.now());
  if (!receipt.ok || !receipt.document) {
    const builtInvoice = tagCashierActor(buildSeasonalWorkPayload(plan, invoiceWork), session);
    return [receipt, rejectDependency(builtInvoice, `linked purchase receipt was not confirmed: ${receipt.reason || 'unknown'}`)];
  }
  logVoucherBuild('voucher_build_begin', invoiceWork);
  const invoiceBuildStart = Date.now();
  const builtInvoice = tagCashierActor(buildSeasonalWorkPayload(plan, invoiceWork, { receiptDocument: receipt.document }), session);
  logVoucherBuild('voucher_build_end', invoiceWork, {
    client_voucher_id: builtInvoice.clientVoucherId,
    build_ms: Math.max(0, Date.now() - invoiceBuildStart),
  });
  emitVoucherStart(builtInvoice);
  const invoice = resolveVoucherAttempts(session, settings, builtInvoice, null, Date.now());
  return [receipt, invoice];
}

export function submitSeasonalVoucher(session, plan, work) {
  if (work.type === 'purchase_cycle') {
    return submitPurchaseCycle(session, plan, work);
  }
  logVoucherBuild('voucher_build_begin', work);
  const buildStart = Date.now();
  const built = tagCashierActor(buildSeasonalWorkPayload(plan, work), session);
  logVoucherBuild('voucher_build_end', work, {
    client_voucher_id: built.clientVoucherId,
    build_ms: Math.max(0, Date.now() - buildStart),
  });
  emitVoucherStart(built);
  return resolveVoucherAttempts(session, plan.settings, built, null, Date.now());
}

export function performContentionBurst(session, plan, work) {
  const settings = plan.settings;
  const warehouses = plan.warehouses.all;
  const distribution = plan.warehouses.distribution;
  const retail = plan.warehouses.retail;
  const sourceWarehouse = distribution[0];
  const probeLines = deterministicLines(
    manifestContext,
    sourceWarehouse,
    1,
    `${settings.runId}:${settings.seed}:${work.segment}:contention-hot:${work.minute}`,
    { distribution: true, includeRates: false }
  );
  const hotItem = probeLines[0].item_code;
  const targetWarehouse = choose(retail, work.minute);
  const submissions = warehouses.map((warehouse, shopIndex) => {
    const saleWork = Object.assign({}, work, {
      mixKey: 'contention_sale',
      type: 'sale',
      lineCount: 10,
      // Preserve the fractional-minute burst index in the client identity while
      // keeping each shop distinct inside that burst.
      sequence: contentionSaleSequence(work.sequence, shopIndex),
      contention: true,
    });
    const builtSale = tagCashierActor(buildSeasonalWorkPayload(plan, saleWork, {
      warehouse,
      requiredItemCode: warehouse === sourceWarehouse ? hotItem : undefined,
    }), session);
    if (warehouse === sourceWarehouse) {
      builtSale.work.sharedContentionItem = hotItem;
    }
    return builtSale;
  });
  const transferWork = Object.assign({}, work, {
    mixKey: 'contention_transfer',
    type: 'transfer',
    lineCount: 50,
    sequence: contentionTransferSequence(work.sequence),
    contention: true,
  });
  const builtTransfer = tagCashierActor(buildSeasonalWorkPayload(plan, transferWork, {
    warehouse: sourceWarehouse,
    sourceWarehouse,
    targetWarehouse,
    requiredItemCode: hotItem,
  }), session);
  builtTransfer.work.sharedContentionItem = hotItem;
  submissions.push(builtTransfer);

  const requests = submissions.map((built) => {
    emitVoucherStart(built);
    const request = postMethodRequest(session, endpointFor(built.work.type), built.payload, {
      timeout: '120s',
      tags: {
        name: `seasonal_${built.work.type}`,
        endpoint: `seasonal_${built.work.type}`,
        voucher_type: built.work.type,
        phase: built.work.phase,
        contention: 'true',
      },
    });
    return { built, request };
  });

  const uxStart = Date.now();
  sleepForSimulatedRtt(1);
  const responses = http.batch(requests.map((entry) => ({
    method: entry.request.method,
    url: entry.request.url,
    body: entry.request.body,
    params: entry.request.params,
  })));
  return responses.map((response, index) => resolveVoucherAttempts(
    session,
    settings,
    requests[index].built,
    {
      response,
      serverDurationMs: response && response.timings && Number.isFinite(response.timings.duration) ? response.timings.duration : 0,
    },
    uxStart
  ));
}

function reportWindow(plan, fullYear = false) {
  const windows = plan && plan.reportWindows ? plan.reportWindows : {};
  const window = fullYear ? windows.full_year : windows.year_to_date;
  if (!window || !window.from_date || !window.to_date) {
    throw new Error(`Missing explicit ${fullYear ? 'full-year' : 'year-to-date'} report window in the seasonal plan`);
  }
  return window;
}

function windowEventFields(plan, fullYear = false) {
  const window = reportWindow(plan, fullYear);
  return {
    window_scope: window.scope,
    window_from_date: window.from_date,
    window_to_date: window.to_date,
  };
}

export function reportFilters(groupBy, fullYear = false, plan = null) {
  const window = reportWindow(plan, fullYear);
  return {
    company: __ENV.COMPANY || manifestContext.company,
    from_date: window.from_date,
    to_date: window.to_date,
    group_by: groupBy,
  };
}

function executeReportCall(session, reportName, filters, timeout, extraPayload = {}, endpointClass = 'manager_summary') {
  const uxStart = Date.now();
  sleepForSimulatedRtt(1);
  const serverStart = Date.now();
  const response = postMethod(
    session,
    'frappe.desk.query_report.run',
    Object.assign({
      report_name: reportName,
      filters: JSON.stringify(filters),
      ignore_prepared_report: 1,
    }, extraPayload),
    {
      asForm: true,
      timeout,
      tags: { name: 'seasonal_query_report', endpoint: 'seasonal_query_report', report: reportName },
      endpointClass,
    }
  );
  return {
    response,
    durationMs: Math.max(0, Date.now() - uxStart),
    serverDurationMs: Math.max(0, Date.now() - serverStart),
  };
}

function validReportRows(response) {
  if (!responseOk(response)) {
    return { ok: false, rows: [], reason: `HTTP ${response ? response.status : 'no response'} or Frappe error` };
  }
  const message = messageBody(response);
  if (!message || message.error || message.prepared_report || message.prepare_report || !Array.isArray(message.result)) {
    return { ok: false, rows: [], reason: 'report did not return a synchronous result array' };
  }
  if (!message.result.length) {
    return { ok: false, rows: [], reason: 'report returned zero rows' };
  }
  let digest = '';
  try {
    digest = canonicalHash([message.columns, message.result]);
  } catch (err) {
    return { ok: false, rows: [], reason: `report result could not be canonically hashed: ${err && err.message ? err.message : err}` };
  }
  return { ok: true, rows: message.result, reason: '', digest };
}

export function performSummaryReport(session, plan, phase, groupBy, iterationIndex) {
  const reportName = plan.settings.summaryReportName;
  const measured = executeReportCall(session, reportName, reportFilters(groupBy, false, plan), '10s', {}, 'manager_summary');
  const validation = validReportRows(measured.response);
  const tags = managerTags(phase.phase, { report: reportName, group_by: groupBy });
  if (validation.ok) {
    reportDuration.add(measured.durationMs, tags);
    reportServerDuration.add(measured.serverDurationMs, tags);
  } else {
    errors.add(1);
    errorRate.add(1);
  }
  logSeasonalEvent({
    event: validation.ok ? 'manager_summary' : 'manager_summary_reject',
    phase: phase.phase,
    segment: phase.name,
    report_name: reportName,
    group_by: groupBy,
    actor: groupBy === 'Warehouse' ? 'summary_warehouse' : 'summary_monthly',
    actor_user: session.user,
    window_scope: windowEventFields(plan, false).window_scope,
    window_from_date: windowEventFields(plan, false).window_from_date,
    window_to_date: windowEventFields(plan, false).window_to_date,
    warm: iterationIndex > 0,
    duration_ms: measured.durationMs,
    row_count: validation.rows.length,
    result_sha256: validation.digest || '',
    hash_algorithm: 'sha256',
    hash_canonicalization: 'canonical-json-v1([columns,result]): recursive object keys sorted; array row order preserved; max 15 fractional digits; no whitespace',
    observation_id: execution.scenario.name + '/' + execution.scenario.iterationInTest,
    reason: validation.reason,
  });
  return validation.ok;
}

function enqueueExplicitExport(session, plan, phase, reason) {
  const response = postMethod(
    session,
    plan.settings.exportEnqueueMethod,
    { filters: reportFilters('Invoice', false, plan) },
    { timeout: '30s', tags: { name: 'seasonal_detail_export_fallback', endpoint: 'seasonal_detail_export_fallback' } }
  );
  const message = messageBody(response);
  const exportLogName = message.export_log_name || message.name || '';
  const ok = responseOk(response) && Boolean(exportLogName);
  if (ok) {
    seasonalDetailFallbacks.add(1, managerTags(phase.phase));
  }
  logSeasonalEvent({
    event: ok ? 'detail_fallback_export' : 'detail_fallback_export_reject',
    phase: phase.phase,
    segment: phase.name,
    export_log_name: exportLogName,
    reason: ok ? reason : `HTTP ${response ? response.status : 'no response'} or missing export_log_name`,
  });
  return { ok, exportLogName };
}

function firstRowIdentity(row) {
  if (!row || typeof row !== 'object') {
    return JSON.stringify(row);
  }
  return JSON.stringify([row.sales_invoice, row.parent_invoice, row.indent, row.posting_date, row.warehouse, row.item_code, row.qty]);
}

export function performDetailReport(session, plan, phase) {
  const settings = plan.settings;
  const reportName = settings.detailReportName;
  const filters = reportFilters('Invoice', false, plan);
  const started = Date.now();
  let firstPage = null;
  let firstValidation = null;
  let pagesRead = 0;
  let failureReason = '';
  let paginationHonored = true;
  const pageSignatures = [];
  const pageDigests = [];
  for (let page = 0; page < settings.detailPageCount; page += 1) {
    const pageFilters = Object.assign({}, filters, { invoice_page: page + 1 });
    const measured = executeReportCall(session, reportName, pageFilters, `${settings.detailTimeoutSeconds}s`, {}, 'manager_detail');
    const validation = validReportRows(measured.response);
    if (page === 0) {
      firstPage = measured;
      firstValidation = validation;
      if (validation.ok) {
        seasonalDetailFirstRowsDuration.add(measured.durationMs, managerTags(phase.phase, { report: reportName }));
      }
    }
    if (!validation.ok) {
      failureReason = validation.reason;
      break;
    }
    if (validation.rows.length > 50) {
      paginationHonored = false;
      failureReason = `pagination not honored: detail endpoint ignored invoice_page bounded rows and returned ${validation.rows.length} rows`;
      break;
    }
    const signature = firstRowIdentity(validation.rows[0]);
    if (pageSignatures.includes(signature)) {
      paginationHonored = false;
      failureReason = 'detail endpoint returned the same first row for consecutive pages; pagination appears ignored';
      break;
    }
    pageSignatures.push(signature);
    pageDigests.push(validation.digest);
    pagesRead += 1;
  }
  const ok = pagesRead === settings.detailPageCount && firstValidation && firstValidation.ok && paginationHonored;
  const durationMs = Math.max(0, Date.now() - started);
  if (!ok) {
    errors.add(1);
    errorRate.add(1);
    enqueueExplicitExport(session, plan, phase, failureReason || 'detail page failed');
  }
  logSeasonalEvent({
    event: ok ? 'manager_detail' : 'manager_detail_reject',
    phase: phase.phase,
    segment: phase.name,
    report_name: reportName,
    actor: 'detail_reader',
    actor_user: session.user,
    window_scope: windowEventFields(plan, false).window_scope,
    window_from_date: windowEventFields(plan, false).window_from_date,
    window_to_date: windowEventFields(plan, false).window_to_date,
    duration_ms: durationMs,
    first_50_duration_ms: firstPage ? firstPage.durationMs : null,
    first_page_sha256: pageDigests.length ? pageDigests[0] : '',
    hash_algorithm: 'sha256',
    hash_canonicalization: 'canonical-json-v1([columns,result]): recursive object keys sorted; array row order preserved; max 15 fractional digits; no whitespace',
    first_page_rows: firstValidation ? firstValidation.rows.length : 0,
    pages_read: pagesRead,
    result_sha256: pageDigests.length ? pageDigests[0] : '',
    all_pages_sha256: canonicalHash(pageDigests),
    observation_id: execution.scenario.name + '/' + execution.scenario.iterationInTest,
    requested_pages: settings.detailPageCount,
    pagination_honored: paginationHonored,
    fallback_used: !ok,
    reason: failureReason,
  });
  return ok;
}

function extractExportLogName(message) {
  if (!message || typeof message !== 'object') {
    return '';
  }
  if (message.export_log_name || message.name) {
    return message.export_log_name || message.name;
  }
  if (message.export_log && typeof message.export_log === 'object') {
    return message.export_log.name || message.export_log.export_log_name || '';
  }
  return '';
}

function headerValue(response, wanted) {
  const headers = response && response.headers ? response.headers : {};
  const lower = wanted.toLowerCase();
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === lower) {
      const value = headers[key];
      return Array.isArray(value) ? String(value[0] || '') : String(value || '');
    }
  }
  return '';
}

function csvEvidence(body) {
  const rowCount = body ? Math.max(0, body.split(/\r?\n/).filter((line) => line.length > 0).length - 1) : 0;
  return {
    rowCount,
    bodyLength: body.length,
    sha256: body ? crypto.sha256(body, 'hex') : '',
  };
}


export function verifyUnauthorizedDetailDenied(session, plan) {
  const response = postMethod(
    session,
    'frappe.desk.query_report.run',
    {
      report_name: plan.settings.detailReportName,
      filters: JSON.stringify(reportFilters('Invoice', false, plan)),
      ignore_prepared_report: 1,
    },
    {
      asForm: true,
      timeout: `${plan.settings.detailTimeoutSeconds}s`,
      tags: { name: 'preflight_unauthorized_detail', endpoint: 'seasonal_query_report', report: plan.settings.detailReportName },
      endpointClass: 'manager_detail',
    }
  );
  const denied = Boolean(response) && (response.status === 401 || response.status === 403);
  logSeasonalEvent({
    event: denied ? 'preflight_unauthorized_detail_denied' : 'preflight_unauthorized_detail_failure',
    actor: 'cashier',
    actor_user: session.user,
    status: response ? response.status : null,
    reason: denied ? 'unauthorized actor denied as required' : 'unauthorized actor was not denied with HTTP 401/403',
  });
  return denied;
}

export function performExportCycle(session, plan, phase) {
  const settings = plan.settings;
  const exportId = hashHex(`${settings.runId}:export:${Date.now()}`).slice(0, 12);
  const started = Date.now();
  logSeasonalEvent({
    event: 'export_start',
    phase: phase.phase,
    segment: phase.name,
    export_cycle: exportId,
    actor: 'export_requester',
    actor_user: session.user,
    window_scope: windowEventFields(plan, true).window_scope,
    window_from_date: windowEventFields(plan, true).window_from_date,
    window_to_date: windowEventFields(plan, true).window_to_date,
  });
  function reject(reason, extra = {}) {
    const durationMs = Math.max(0, Date.now() - started);
    errors.add(1);
    errorRate.add(1);
    logSeasonalEvent(Object.assign({
      event: 'export_reject',
      phase: phase.phase,
      segment: phase.name,
      export_cycle: exportId,
      duration_ms: durationMs,
      reason,
    }, extra));
    sleep(5);
    return { ok: false, reason };
  }

  const enqueueResponse = postMethod(
    session,
    settings.exportEnqueueMethod,
    { filters: reportFilters('Invoice', true, plan) },
    { timeout: '30s', tags: { name: 'seasonal_export_enqueue', endpoint: 'seasonal_export_enqueue' } }
  );
  const enqueueMessage = messageBody(enqueueResponse);
  const exportLogName = extractExportLogName(enqueueMessage);
  if (!responseOk(enqueueResponse) || !exportLogName) {
    return reject(`export enqueue failed or returned no export_log_name (HTTP ${enqueueResponse ? enqueueResponse.status : 0})`);
  }

  const timeoutSeconds = Number(__ENV.SEASONAL_EXPORT_TIMEOUT_SECONDS || 600);
  const deadline = Date.now() + timeoutSeconds * 1000;
  const downloadQuery = `${settings.exportDownloadMethod}?export_log_name=${encodeURIComponent(exportLogName)}`;
  let downloadResponse = getMethod(
    session,
    downloadQuery,
    { timeout: '180s', tags: { name: 'seasonal_export_status', endpoint: 'seasonal_export_status' } }
  );
  const initialStatus = downloadResponse ? downloadResponse.status : 0;
  logSeasonalEvent({
    event: initialStatus === 200 || initialStatus === 409 ? 'export_status' : 'export_status_reject',
    phase: phase.phase,
    segment: phase.name,
    export_cycle: exportId,
    export_log_name: exportLogName,
    http_status: initialStatus,
    status_semantics: initialStatus === 409 ? 'queued_or_running' : (initialStatus === 200 ? 'completed_downloadable' : 'unexpected'),
  });
  if (initialStatus !== 200 && initialStatus !== 409) {
    return reject(`export status probe returned unexpected HTTP ${initialStatus}`, { export_log_name: exportLogName });
  }
  while (Date.now() < deadline && (!downloadResponse || downloadResponse.status !== 200)) {
    downloadResponse = getMethod(
      session,
      downloadQuery,
      { timeout: '180s', tags: { name: 'seasonal_export_download', endpoint: 'seasonal_export_download' } }
    );
    if (downloadResponse && downloadResponse.status === 200) {
      break;
    }
    if (!downloadResponse || downloadResponse.status !== 409) {
      return reject(
        `export download polling reached unexpected HTTP ${downloadResponse ? downloadResponse.status : 0}`,
        { export_log_name: exportLogName }
      );
    }
    sleep(Number(__ENV.SEASONAL_EXPORT_POLL_SECONDS || 5));
  }
  if (!downloadResponse || downloadResponse.status !== 200) {
    return reject(`export ${exportLogName} did not become downloadable within ${timeoutSeconds}s`, { export_log_name: exportLogName });
  }

  const body = responseBodyText(downloadResponse);
  const evidence = csvEvidence(body);
  const contentType = headerValue(downloadResponse, 'content-type').toLowerCase();
  const disposition = headerValue(downloadResponse, 'content-disposition').toLowerCase();
  const downloadOk =
    evidence.bodyLength > 0 &&
    evidence.rowCount > 0 &&
    (contentType.includes('text/csv') || disposition.includes('attachment'));
  if (!downloadOk) {
    return reject(
      `export download did not provide non-empty CSV evidence (rows=${evidence.rowCount}, bytes=${evidence.bodyLength})`,
      { export_log_name: exportLogName }
    );
  }
  const durationMs = Math.max(0, Date.now() - started);
  seasonalExportDuration.add(durationMs, managerTags(phase.phase));
  seasonalExportBytes.add(evidence.bodyLength, managerTags(phase.phase));
  seasonalExportRows.add(evidence.rowCount, managerTags(phase.phase));
  logSeasonalEvent({
    event: 'export_complete',
    phase: phase.phase,
    segment: phase.name,
    export_cycle: exportId,
    export_log_name: exportLogName,
    actor: 'export_requester',
    actor_user: session.user,
    window_scope: windowEventFields(plan, true).window_scope,
    window_from_date: windowEventFields(plan, true).window_from_date,
    window_to_date: windowEventFields(plan, true).window_to_date,
    duration_ms: durationMs,
    body_bytes: evidence.bodyLength,
    row_count: evidence.rowCount,
    sha256: evidence.sha256,
    hash_algorithm: 'sha256',
    hash_canonicalization: 'raw-export-response-text-v1',
    observation_id: execution.scenario.name + '/' + execution.scenario.iterationInTest,
    content_type: contentType,
  });
  return { ok: true, exportLogName, rowCount: evidence.rowCount, bodyLength: evidence.bodyLength, sha256: evidence.sha256 };
}

export function performSeasonalSearch(session, plan, phase, arrivalIndex) {
  const warehouses = plan.warehouses.all;
  const warehouse = choose(warehouses, arrivalIndex);
  const seedKey = `${plan.settings.runId}:${plan.settings.seed}:search:${phase.name}:${arrivalIndex}`;
  const lines = deterministicLines(manifestContext, warehouse, 1, seedKey, {
    distribution: plan.warehouses.distribution.includes(warehouse),
    includeRates: false,
  });
  const itemCode = lines[0].item_code;
  const exact = stableHash32(seedKey) % 4 === 0;
  const term = exact ? itemCode : itemCode.slice(0, 3 + (stableHash32(`${seedKey}:len`) % Math.max(1, Math.min(7, itemCode.length - 2))));
  const uxStart = Date.now();
  sleepForSimulatedRtt(1);
  const serverStart = Date.now();
  const query =
    `khitmee_ops.api.search.search_items?q=${encodeURIComponent(term)}` +
    `&warehouse=${encodeURIComponent(warehouse)}` +
    `&price_list=${encodeURIComponent(manifestContext.defaultPriceList)}` +
    '&limit=15';
  const response = getMethod(session, query, { tags: { name: 'seasonal_search', endpoint: 'seasonal_search' } });
  const serverDurationMs = Math.max(0, Date.now() - serverStart);
  const durationMs = Math.max(0, Date.now() - uxStart);
  const message = messageBody(response);
  const ok = responseOk(response) && Array.isArray(message.items) && message.items.length > 0;
  const tags = managerTags(phase.phase, { warehouse });
  if (ok) {
    searchDuration.add(durationMs, tags);
    searchServerDuration.add(serverDurationMs, tags);
  } else {
    errors.add(1);
    errorRate.add(1);
  }
  logSeasonalEvent({
    event: ok ? 'search_complete' : 'search_reject',
    phase: phase.phase,
    segment: phase.name,
    actor: 'cashier',
    actor_user: session.user,
    warehouse,
    exact,
    arrival_index: arrivalIndex,
    term_length: term.length,
    duration_ms: durationMs,
    result_count: Array.isArray(message.items) ? message.items.length : null,
    reason: ok ? '' : 'cashier search endpoint failed or returned no rows',
  });
  return ok;
}

export function performCashierDiscovery(session, plan) {
  const segment = Object.assign({}, plan.segments[0], { name: 'discovery', phase: 'discovery' });
  const ok = performSeasonalSearch(session, plan, segment, 0);
  logSeasonalEvent({
    event: 'cashier_discovery',
    passed: ok,
    discovery_scope: 'login_and_real_counter_search',
    endpoint: 'khitmee_ops.api.search.search_items',
    reason: ok ? '' : 'authenticated cashier search did not return rows',
  });
  return ok;
}

export function phaseAt(plan, elapsedSeconds) {
  const elapsed = Math.max(0, elapsedSeconds);
  for (const segment of plan.segments) {
    if (elapsed < segment.startSeconds + segment.durationSeconds) {
      return segment;
    }
  }
  return plan.segments[plan.segments.length - 1];
}
