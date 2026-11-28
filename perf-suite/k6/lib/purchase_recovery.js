const PURCHASE_DOCTYPES = {
  purchase_receipt: 'Purchase Receipt',
  purchase_invoice: 'Purchase Invoice',
};

const TRANSIENT_DOCUMENT_FIELDS = new Set(['doctype', 'name', '__islocal', 'docstatus', 'items']);
const TRANSIENT_CHILD_FIELDS = new Set(['doctype', 'name', '__islocal', 'docstatus', 'parent', 'parenttype', 'parentfield']);

export function isPurchaseVoucherType(type) {
  return Object.prototype.hasOwnProperty.call(PURCHASE_DOCTYPES, type);
}

export function ambiguousSubmitResponse(response) {
  if (!response) {
    return true;
  }
  if ([0, 408, 409, 425, 429].includes(response.status) || response.status >= 500) {
    return true;
  }
  const text = String(response.body === undefined || response.body === null ? '' : response.body).toLowerCase();
  return text.includes('already being submitted') || text.includes('deadlock') || text.includes('lock wait timeout');
}

export function mayResubmitAfterSubmitResponse(type, response) {
  if (isPurchaseVoucherType(type)) {
    return false;
  }
  return ambiguousSubmitResponse(response);
}

export function purchaseLookupSpec(built) {
  const type = built && built.work && built.work.type;
  const doctype = PURCHASE_DOCTYPES[type];
  const clientVoucherId = built && built.clientVoucherId;
  const document = built && built.document;
  if (!doctype || !clientVoucherId || !document) {
    throw new Error('purchase recovery lookup requires the original client voucher identity and document');
  }
  const filters = type === 'purchase_invoice'
    ? [[doctype, 'bill_no', '=', clientVoucherId]]
    : [[doctype, 'remarks', '=', document.remarks]];
  return {
    doctype,
    clientVoucherId,
    filters,
    fields: ['name'],
    limitPageLength: 2,
  };
}

function fail(reason) {
  return { ok: false, reason };
}

function valuesEqual(expected, actual) {
  if (typeof expected === 'number') {
    if (typeof actual !== 'number' && !(typeof actual === 'string' && actual.trim() !== '')) {
      return false;
    }
    const number = Number(actual);
    return Number.isFinite(number) && number === expected;
  }
  return actual === expected;
}

function validateSimpleFields(actual, expected, transientFields, label) {
  for (const key of Object.keys(expected)) {
    if (transientFields.has(key) || expected[key] === undefined || (typeof expected[key] === 'object' && expected[key] !== null)) {
      continue;
    }
    if (!valuesEqual(expected[key], actual[key])) {
      return fail(`${label} field ${key} does not match the original purchase document`);
    }
  }
  return { ok: true };
}

function validateItems(actualItems, expectedItems, type) {
  if (!Array.isArray(actualItems) || actualItems.length !== expectedItems.length) {
    return fail(`recovered document has ${Array.isArray(actualItems) ? actualItems.length : 0} item rows, expected ${expectedItems.length}`);
  }
  for (let index = 0; index < expectedItems.length; index += 1) {
    const expected = expectedItems[index];
    const actual = actualItems[index] || {};
    const scalar = validateSimpleFields(actual, expected, TRANSIENT_CHILD_FIELDS, `item row ${index + 1}`);
    if (!scalar.ok) {
      return scalar;
    }
    if (type === 'purchase_invoice') {
      if (actual.purchase_receipt !== expected.purchase_receipt || actual.pr_detail !== expected.pr_detail) {
        return fail(`item row ${index + 1} is not linked to the original Purchase Receipt row`);
      }
    }
  }
  return { ok: true };
}

export function validateRecoveredPurchaseDocument(doc, built) {
  const type = built && built.work && built.work.type;
  const expectedDoctype = PURCHASE_DOCTYPES[type];
  const expected = built && built.document;
  if (!expectedDoctype || !expected) {
    return fail('missing original purchase document');
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) {
    return fail('recovered purchase document is unreadable');
  }
  if (!doc.name) {
    return fail('recovered purchase document has no server name');
  }
  if (doc.doctype && doc.doctype !== expectedDoctype) {
    return fail(`expected ${expectedDoctype}, recovered ${doc.doctype}`);
  }
  if (Number(doc.docstatus) !== 1) {
    return fail(`recovered ${expectedDoctype} docstatus is ${doc.docstatus}, expected 1`);
  }
  if (String(doc.remarks || '') !== String(expected.remarks || '')) {
    return fail('recovered purchase document remarks do not exactly match the original client marker');
  }
  if (type === 'purchase_invoice' && doc.bill_no !== built.clientVoucherId) {
    return fail('recovered Purchase Invoice bill_no does not match the original client voucher identity');
  }
  const scalar = validateSimpleFields(doc, expected, TRANSIENT_DOCUMENT_FIELDS, expectedDoctype);
  if (!scalar.ok) {
    return scalar;
  }
  const items = validateItems(doc.items, expected.items || [], type);
  if (!items.ok) {
    return items;
  }
  return { ok: true, voucherNo: doc.name, document: doc };
}
