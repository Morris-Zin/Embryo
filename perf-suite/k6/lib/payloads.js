import { SharedArray } from 'k6/data';

function seedManifestUrls() {
  if (__ENV.SEED_MANIFEST_PATH) {
    return [__ENV.SEED_MANIFEST_PATH];
  }
  const candidates = ['../../seed_manifest.json', '../../seeder/seed_manifest.json', '../seed_manifest.json'];
  if (import.meta && typeof import.meta.resolve === 'function') {
    return candidates.map((path) => import.meta.resolve(path));
  }
  return candidates;
}

function loadSeedManifest() {
  const paths = seedManifestUrls();
  const failures = [];
  for (let index = 0; index < paths.length; index += 1) {
    try {
      return JSON.parse(open(paths[index]));
    } catch (err) {
      failures.push(`${paths[index]} (${err})`);
    }
  }
  throw new Error(`Unable to load seed manifest. Tried: ${failures.join('; ')}`);
}

function compactSeasonalContext(full) {
  const inStock = {};
  const stocked = {};
  Object.keys(full.inStock).forEach((warehouseKey) => {
    const entry = full.inStock[warehouseKey];
    const itemCodes = [];
    const quantities = [];
    const uoms = [];
    entry.items.forEach((item) => {
      itemCodes.push(item.item_code);
      quantities.push(item.qty);
      uoms.push(item.uom);
      stocked[item.item_code] = true;
    });
    inStock[warehouseKey] = {
      warehouse: entry.warehouse,
      count: itemCodes.length,
      item_codes: itemCodes,
      quantities,
      uoms,
    };
  });
  const catalogByCode = {};
  const hasStockMap = Object.keys(full.inStock || {}).length > 0;
  full.items.forEach((item) => {
    if ((!hasStockMap || stocked[item.item_code]) && catalogByCode[item.item_code] === undefined) {
      catalogByCode[item.item_code] = item;
    }
  });
  const catalogCodes = Object.keys(catalogByCode).sort();
  const catalogRates = [];
  const catalogHasRate = [];
  const catalogNames = [];
  const catalogBarcodes = [];
  const catalogUoms = [];
  const catalogHotRanks = [];
  catalogCodes.forEach((code) => {
    const item = catalogByCode[code];
    catalogRates.push(item.rate === undefined ? 0 : item.rate);
    catalogHasRate.push(item.rate !== undefined);
    catalogNames.push(item.item_name || code);
    catalogBarcodes.push(item.barcode || '');
    catalogUoms.push(item.uom || full.defaultUom || 'Nos');
    catalogHotRanks.push(item.hot_rank || 0);
  });
  const catalogHotOrder = catalogCodes.map((code, index) => index).sort((left, right) => {
    const rankDelta = (catalogHotRanks[left] || 0) - (catalogHotRanks[right] || 0);
    return rankDelta || left - right;
  });
  return {
    raw: { seed_id: full.raw && full.raw.seed_id, site_name: full.raw && full.raw.site_name },
    items: catalogCodes.slice(),
    inStock,
    warehouses: full.warehouses,
    priceLists: full.priceLists,
    customers: full.customers,
    suppliers: full.suppliers,
    uoms: full.uoms,
    company: full.company,
    catalogCodes,
    catalogRates,
    catalogHasRate,
    catalogNames,
    catalogBarcodes,
    catalogUoms,
    catalogHotRanks,
    catalogHotOrder,
    reportWindow: full.reportWindow,
    defaultPriceList: full.defaultPriceList,
    defaultCustomer: full.defaultCustomer,
    defaultSupplier: full.defaultSupplier,
    defaultUom: full.defaultUom,
    paymentMode: full.paymentMode,
    defaultCurrency: full.defaultCurrency,
    defaultCashAccount: full.defaultCashAccount,
  };
}

const DEFAULT_WAREHOUSES = [
  { name: 'Dist Hub 1', role: 'distribution' },
  { name: 'Dist Hub 2', role: 'distribution' },
  { name: 'Shop 1', role: 'retail' },
  { name: 'Shop 2', role: 'retail' },
  { name: 'Shop 3', role: 'retail' },
  { name: 'Shop 4', role: 'retail' },
];

const sharedManifestContext = new SharedArray('seed_manifest_context', function () {
  return [compactSeasonalContext(buildContextFromRoot(loadSeedManifest()))];
});

function asList(value) {
  if (!value) {
    return [];
  }
  if (Array.isArray(value)) {
    return value;
  }
  if (typeof value === 'object') {
    return Object.keys(value).map((key) => {
      const item = value[key];
      if (typeof item === 'object' && item !== null) {
        return Object.assign({ key }, item);
      }
      return { key, value: item };
    });
  }
  return [];
}

function firstList() {
  for (let i = 0; i < arguments.length; i += 1) {
    const value = asList(arguments[i]);
    if (value.length) {
      return value;
    }
  }
  return [];
}

function textValue() {
  for (let i = 0; i < arguments.length; i += 1) {
    const value = arguments[i];
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      return String(value);
    }
  }
  return '';
}

function numberValue(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function normalizeItem(entry, index) {
  if (typeof entry === 'string') {
    return { item_code: entry, item_name: entry, uom: 'Nos', hot_rank: index + 1 };
  }
  if (!entry || typeof entry !== 'object') {
    return null;
  }
  const itemCode = textValue(entry.item_code, entry.itemCode, entry.code, entry.sku, entry.name, entry.key);
  if (!itemCode) {
    return null;
  }
  const rank = numberValue(
    entry.hot_rank !== undefined ? entry.hot_rank : entry.sales_rank !== undefined ? entry.sales_rank : entry.rank
  );
  return {
    item_code: itemCode,
    item_name: textValue(entry.item_name, entry.itemName, entry.description, entry.label, itemCode),
    barcode: textValue(entry.barcode, entry.ean, entry.upc),
    uom: textValue(entry.uom, entry.stock_uom, entry.stockUom, 'Nos'),
    rate: numberValue(entry.rate !== undefined ? entry.rate : entry.price),
    hot_rank: rank === undefined ? index + 1 : rank,
  };
}

function inferWarehouseRole(name, explicit) {
  const value = String(explicit || '').toLowerCase();
  if (value.includes('dist') || value.includes('hub') || value.includes('warehouse')) {
    return 'distribution';
  }
  if (value.includes('retail') || value.includes('shop') || value.includes('store')) {
    return 'retail';
  }
  const lowerName = String(name || '').toLowerCase();
  return lowerName.includes('dist') || lowerName.includes('hub') ? 'distribution' : 'retail';
}

function normalizeWarehouse(entry, index) {
  if (typeof entry === 'string') {
    return { name: entry, role: inferWarehouseRole(entry), hot_rank: index + 1 };
  }
  if (!entry || typeof entry !== 'object') {
    return null;
  }
  const logicalName = textValue(entry.key);
  const name = textValue(entry.name, entry.warehouse, entry.warehouse_name, entry.title, entry.value, logicalName);
  if (!name) {
    return null;
  }
  return {
    name,
    logical_name: logicalName,
    role: inferWarehouseRole(logicalName || name, entry.role || entry.type || entry.kind),
    company: textValue(entry.company),
  };
}

function scalarList(value, fallback) {
  const list = asList(value)
    .map((entry) => {
      if (typeof entry === 'string') {
        return entry;
      }
      if (entry && typeof entry === 'object') {
        return textValue(entry.name, entry.price_list, entry.customer, entry.uom, entry.key, entry.value);
      }
      return textValue(entry);
    })
    .filter(Boolean);
  return list.length ? list : fallback;
}

function normalizeInStockEntry(entry, index) {
  if (typeof entry === 'string') {
    return { item_code: entry, item_name: entry, uom: 'Nos', hot_rank: index + 1 };
  }
  if (!entry || typeof entry !== 'object') {
    return null;
  }
  const itemCode = textValue(entry.item_code, entry.itemCode, entry.code, entry.sku, entry.name, entry.key);
  if (!itemCode) {
    return null;
  }
  return {
    item_code: itemCode,
    item_name: textValue(entry.item_name, entry.itemName, entry.description, entry.label, itemCode),
    barcode: textValue(entry.barcode, entry.ean, entry.upc),
    uom: textValue(entry.uom, entry.stock_uom, entry.stockUom, 'Nos'),
    qty: numberValue(
      entry.qty !== undefined ? entry.qty : entry.available_qty !== undefined ? entry.available_qty : entry.stock_qty
    ),
    hot_rank: index + 1,
  };
}

function normalizeInStockMap(raw) {
  const result = {};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return result;
  }
  Object.keys(raw).forEach((warehouseName) => {
    const items = asList(raw[warehouseName]).map(normalizeInStockEntry).filter(Boolean);
    if (items.length) {
      result[String(warehouseName).toLowerCase()] = { warehouse: warehouseName, items };
    }
  });
  return result;
}

function manifestItems(root) {
  const directItems = firstList(root.item_master, root.itemCodes, root.item_codes, root.skus, root.active_items, root.catalog);
  if (directItems.length) {
    return directItems.map(normalizeItem).filter(Boolean);
  }
  const section = root.items;
  if (Array.isArray(section) && section.length) {
    // Explicit item list from the seeder contract; never reconstruct synthetic codes.
    return section.map(normalizeItem).filter(Boolean);
  }
  if (section && typeof section === 'object') {
    const count = Number(section.count || 0);
    const hotSamples = asList(section.hot_samples).map(String);
    const coldSamples = asList(section.cold_samples).map(String);
    const barcodeMap = section.barcodes && typeof section.barcodes === 'object' ? section.barcodes : {};
    const samples = hotSamples.concat(coldSamples);
    const synthetic = samples.some((code) => /^KM-\d+$/.test(code));
    const inStock = normalizeInStockMap(root.in_stock_items);
    if (synthetic && count > 0 && Object.keys(inStock).length === 0) {
      // Legacy fallback only: reconstruct the catalog when no explicit items exist at all.
      const width = Math.max(5, String(count).length);
      const generated = [];
      for (let index = 1; index <= count; index += 1) {
        const code = `KM-${String(index).padStart(width, '0')}`;
        generated.push({
          item_code: code,
          item_name: code,
          barcode: textValue(barcodeMap[code]),
          uom: 'Nos',
          hot_rank: index,
        });
      }
      return generated;
    }
    if (samples.length) {
      return samples.map((code, index) => ({
        item_code: code,
        item_name: code,
        barcode: textValue(barcodeMap[code]),
        uom: 'Nos',
        hot_rank: index + 1,
      }));
    }
  }
  const inStock = normalizeInStockMap(root.in_stock_items);
  const seen = {};
  const derived = [];
  Object.keys(inStock).forEach((key) => {
    inStock[key].items.forEach((item) => {
      if (!seen[item.item_code]) {
        seen[item.item_code] = true;
        derived.push(item);
      }
    });
  });
  return derived;
}

function buildContextFromRoot(rawRoot) {
  const root = Array.isArray(rawRoot) ? { items: rawRoot } : rawRoot || {};
  const expectedSite = String(__ENV.SITE_NAME || 'bench-loadtest.localhost');
  if (root.site_name && String(root.site_name) !== expectedSite) {
    throw new Error(
      `Seed manifest site_name '${root.site_name}' does not match the target site '${expectedSite}'. ` +
        'Refusing to run with a mismatched manifest.'
    );
  }
  const items = manifestItems(root)
    .filter(Boolean)
    .sort((a, b) => a.hot_rank - b.hot_rank);
  const warehouses = firstList(root.warehouses, root.warehouse_list, root.shops, root.locations)
    .map(normalizeWarehouse)
    .filter(Boolean);
  const priceLists = scalarList(root.price_lists || root.priceLists, [textValue(root.price_list, 'Standard Selling')]);
  const customers = scalarList(root.customers || root.customer_list, ['Walk-in Customer']);
  const suppliers = scalarList(root.suppliers || root.supplier_list, []);
  const uoms = scalarList(root.uoms || root.units, ['Nos']);
  const company = textValue(root.company, root.company_name, warehouses.find((warehouse) => warehouse.company)?.company, 'Khitmee');
  const reportWindowRaw = root.report_window || root.reportWindow || {};
  const catalogLookup = {};
  items.forEach((item) => {
    if (catalogLookup[item.item_code] === undefined) {
      catalogLookup[item.item_code] = item;
    }
  });

  return {
    raw: root,
    items,
    inStock: normalizeInStockMap(root.in_stock_items),
    warehouses: warehouses.length ? warehouses : DEFAULT_WAREHOUSES.slice(),
    priceLists,
    customers,
    suppliers,
    uoms,
    company,
    catalogLookup,
    reportWindow: {
      from_date: textValue(reportWindowRaw.from_date, reportWindowRaw.fromDate),
      to_date: textValue(reportWindowRaw.to_date, reportWindowRaw.toDate),
      final_voucher_date: textValue(reportWindowRaw.final_voucher_date, reportWindowRaw.finalVoucherDate),
    },
    defaultPriceList: textValue(root.default_price_list, root.price_list, priceLists[0], 'Standard Selling'),
    defaultCustomer: textValue(root.default_customer, customers[0], 'Walk-in Customer'),
    defaultSupplier: textValue(root.default_supplier, suppliers[0]),
    defaultUom: textValue(root.default_uom, uoms[0], 'Nos'),
    paymentMode: textValue(root.payment_mode, root.paymentMode, 'Cash'),
    defaultCurrency: textValue(root.currency, root.company_currency, root.default_currency, 'USD'),
    defaultCashAccount: textValue(root.cash_account, root.cashAccount),
  };
}

export const manifestContext = sharedManifestContext[0];

const zipfCache = {};

function zipfCdf(length, exponent) {
  const key = `${length}:${exponent}`;
  if (zipfCache[key]) {
    return zipfCache[key];
  }
  const weights = [];
  let total = 0;
  for (let rank = 1; rank <= length; rank += 1) {
    total += 1 / Math.pow(rank, exponent);
    weights.push(total);
  }
  const cdf = weights.map((value) => value / total);
  zipfCache[key] = cdf;
  return cdf;
}

export function zipfIndex(length, exponent = 1.2) {
  if (!length || length < 1) {
    throw new Error('zipfIndex requires a non-empty collection.');
  }
  const cdf = zipfCdf(length, exponent);
  const needle = Math.random();
  let low = 0;
  let high = cdf.length - 1;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (needle <= cdf[mid]) {
      high = mid;
    } else {
      low = mid + 1;
    }
  }
  return low;
}

export function randomIntBetween(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

export function randomItem(list) {
  if (!list || !list.length) {
    throw new Error('Cannot choose from an empty list.');
  }
  return list[Math.floor(Math.random() * list.length)];
}

function catalogRateIndex(itemCode) {
  const codes = manifestContext.catalogCodes || [];
  let low = 0;
  let high = codes.length - 1;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    if (codes[mid] === itemCode) {
      return mid;
    }
    if (codes[mid] < itemCode) {
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return -1;
}

function catalogItemAt(index) {
  const code = manifestContext.catalogCodes[index];
  const item = {
    item_code: code,
    item_name: (manifestContext.catalogNames || [])[index] || code,
    barcode: (manifestContext.catalogBarcodes || [])[index] || '',
    uom: (manifestContext.catalogUoms || [])[index] || manifestContext.defaultUom || 'Nos',
    hot_rank: (manifestContext.catalogHotRanks || [])[index] || index + 1,
  };
  if ((manifestContext.catalogHasRate || [])[index]) {
    item.rate = manifestContext.catalogRates[index];
  }
  return item;
}

function catalogOrder() {
  if (manifestContext.catalogHotOrder && manifestContext.catalogHotOrder.length) {
    return manifestContext.catalogHotOrder;
  }
  return (manifestContext.catalogCodes || []).map((_code, index) => index);
}

export function catalogItems() {
  return catalogOrder().map(catalogItemAt);
}

function inStockItemAt(entry, index) {
  if (entry.items) {
    return entry.items[index];
  }
  const code = (entry.item_codes || [])[index];
  const catalogIndex = catalogRateIndex(code);
  const catalogItem = catalogIndex >= 0 ? catalogItemAt(catalogIndex) : null;
  const item = {
    item_code: code,
    item_name: catalogItem ? catalogItem.item_name : code,
    barcode: catalogItem ? catalogItem.barcode : '',
    qty: (entry.quantities || [])[index],
    uom: (entry.uoms || [])[index] || manifestContext.defaultUom || 'Nos',
    hot_rank: catalogItem ? catalogItem.hot_rank : index + 1,
  };
  if (catalogIndex >= 0 && (manifestContext.catalogHasRate || [])[catalogIndex]) {
    item.rate = manifestContext.catalogRates[catalogIndex];
  }
  return item;
}

export function inStockItemsFor(warehouse) {
  const entry = manifestContext.inStock[String(warehouse || '').toLowerCase()];
  if (!entry) {
    return [];
  }
  const length = entry.items ? entry.items.length : (entry.item_codes || []).length;
  const items = [];
  for (let index = 0; index < length; index += 1) {
    items.push(inStockItemAt(entry, index));
  }
  return items;
}

export function pickItem(options = {}) {
  let length = 0;
  let itemAtIndex = null;
  if (options.warehouse) {
    // Stock-moving flows must only pick items actually stocked in this warehouse.
    const entry = manifestContext.inStock[String(options.warehouse || '').toLowerCase()];
    const stockLength = entry ? (entry.items ? entry.items.length : (entry.item_codes || []).length) : 0;
    if (stockLength) {
      length = stockLength;
      itemAtIndex = (index) => inStockItemAt(entry, index);
    }
  }
  if (!itemAtIndex) {
    const order = catalogOrder();
    length = order.length;
    itemAtIndex = (index) => catalogItemAt(order[index]);
  }
  if (!length) {
    throw new Error('Seed manifest contains no usable items. Expected items with item_code / sku values.');
  }
  const contention = Boolean(options.contention);
  const configuredPool = Number(__ENV.HOT_SKU_POOL || 50);
  const poolSize = contention ? Math.max(1, Math.min(length, configuredPool)) : length;
  const exponent = Number(options.zipfExponent || __ENV.ZIPF_EXPONENT || (contention ? 1.45 : 1.15));
  return itemAtIndex(zipfIndex(poolSize, exponent));
}

export function warehouseRole(warehouseName) {
  const found = manifestContext.warehouses.find((warehouse) => warehouse.name.toLowerCase() === String(warehouseName).toLowerCase());
  return found ? found.role : inferWarehouseRole(warehouseName);
}

function warehouseNamesByRole(role) {
  return manifestContext.warehouses.filter((warehouse) => warehouse.role === role).map((warehouse) => warehouse.name);
}

export function shopWarehouseSequence() {
  if (__ENV.WAREHOUSE_SEQUENCE) {
    return __ENV.WAREHOUSE_SEQUENCE.split(',').map((value) => value.trim()).filter(Boolean);
  }
  const available = manifestContext.warehouses.map((warehouse) => warehouse.name);
  const lowerToActual = {};
  available.forEach((name) => {
    lowerToActual[name.toLowerCase()] = name;
  });
  const exact = DEFAULT_WAREHOUSES.map((entry) => lowerToActual[entry.name.toLowerCase()]).filter(Boolean);
  if (exact.length === DEFAULT_WAREHOUSES.length) {
    return exact;
  }
  const distribution = warehouseNamesByRole('distribution');
  const retail = warehouseNamesByRole('retail');
  if (distribution.length >= 2 && retail.length >= 4) {
    return distribution.slice(0, 2).concat(retail.slice(0, 4));
  }
  return DEFAULT_WAREHOUSES.map((entry) => entry.name);
}

export function chooseTransferTarget(sourceWarehouse) {
  const sourceRole = warehouseRole(sourceWarehouse);
  const distribution = warehouseNamesByRole('distribution');
  const retail = warehouseNamesByRole('retail');
  if (sourceRole === 'distribution' && retail.length) {
    return randomItem(retail);
  }
  if (sourceRole !== 'distribution' && distribution.length) {
    return randomItem(distribution.filter((name) => name !== sourceWarehouse).concat(distribution));
  }
  const alternatives = manifestContext.warehouses.map((warehouse) => warehouse.name).filter((name) => name !== sourceWarehouse);
  return alternatives.length ? randomItem(alternatives) : sourceWarehouse;
}

export function searchTermForItem(item, exact = false) {
  if (!item) {
    return 'item';
  }
  if (exact) {
    return item.barcode || item.item_code;
  }
  if (item.barcode && Math.random() < 0.25) {
    return item.barcode;
  }
  const words = String(item.item_name || '').split(/\s+/).filter((word) => word.length >= 3);
  if (words.length && Math.random() < 0.65) {
    return randomItem(words).slice(0, 12);
  }
  return item.item_code.slice(0, randomIntBetween(3, Math.min(10, item.item_code.length)));
}

function clientVoucherId(kind) {
  return `K6-${kind}-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

function configuredLineCount(distribution) {
  const min = Number(__ENV.MIN_LINES || 3);
  const defaultMax = distribution ? 25 : 12;
  const max = Number(__ENV.MAX_LINES || defaultMax);
  return randomIntBetween(Math.max(1, min), Math.max(Math.max(1, min), Math.min(25, max)));
}

function quantityForLine(distribution) {
  if (distribution) {
    return randomIntBetween(2, 30);
  }
  const roll = Math.random();
  if (roll < 0.72) {
    return randomIntBetween(1, 3);
  }
  if (roll < 0.94) {
    return randomIntBetween(4, 8);
  }
  return randomIntBetween(9, 15);
}

function capQtyForStock(item, desired) {
  const available = numberValue(item.qty);
  if (available === undefined) {
    return desired;
  }
  // Bound each line to a safe per-VU share of the seeded on-hand quantity.
  const divisor = Math.max(1, Number(__ENV.PER_VU_STOCK_DIVISOR || 8));
  const perVuCap = Math.max(1, Math.floor(available / divisor));
  return Math.max(1, Math.min(desired, perVuCap));
}

export function buildLines(options = {}) {
  const distribution = Boolean(options.distribution);
  const hasExplicitCount = options.count !== undefined || options.lineCount !== undefined;
  const count = Number(
    options.count !== undefined ? options.count : options.lineCount !== undefined ? options.lineCount : configuredLineCount(distribution)
  );
  if (!Number.isFinite(count) || count < 1 || Math.floor(count) !== count) {
    throw new Error(`Voucher line count must be a positive integer, got ${count}`);
  }
  const includeRates = options.includeRates !== false;
  const lines = [];
  const seen = {};
  let attempts = 0;
  while (lines.length < count && attempts < count * 20) {
    attempts += 1;
    const item = pickItem(options);
    if (seen[item.item_code]) {
      continue;
    }
    seen[item.item_code] = true;
    const line = {
      item_code: item.item_code,
      qty: capQtyForStock(item, quantityForLine(distribution)),
      uom: item.uom || manifestContext.defaultUom,
    };
    if (includeRates && item.rate !== undefined) {
      line.rate = item.rate;
    }
    lines.push(line);
  }
  if (!lines.length) {
    throw new Error('Unable to build a voucher with at least one item line.');
  }
  if (hasExplicitCount && lines.length !== count) {
    throw new Error(`Unable to build ${count} distinct item lines; only ${lines.length} unique lines were available.`);
  }
  return lines;
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function selectedCustomer() {
  if (__ENV.CUSTOMER) {
    return __ENV.CUSTOMER;
  }
  if (Math.random() < 0.75) {
    return manifestContext.defaultCustomer;
  }
  return randomItem(manifestContext.customers);
}

function selectedPriceList() {
  return __ENV.PRICE_LIST || manifestContext.defaultPriceList;
}

export function buildKhitmeeSalePayload(options = {}) {
  const warehouse = options.warehouse || randomItem(shopWarehouseSequence());
  return {
    client_voucher_id: clientVoucherId('sale'),
    warehouse,
    customer: options.customer || selectedCustomer(),
    price_list: options.priceList || selectedPriceList(),
    items: buildLines({
      distribution: warehouseRole(warehouse) === 'distribution',
      contention: options.contention,
      warehouse,
      lineCount: options.lineCount,
    }),
    payment_mode: options.paymentMode || __ENV.PAYMENT_MODE || 'Cash',
    printer_type: options.printerType || __ENV.PRINTER_TYPE || 'voucher',
  };
}

export function buildKhitmeeTransferPayload(options = {}) {
  const sourceWarehouse = options.sourceWarehouse || randomItem(shopWarehouseSequence());
  const targetWarehouse = options.targetWarehouse || chooseTransferTarget(sourceWarehouse);
  const payload = {
    client_voucher_id: clientVoucherId('transfer'),
    source_warehouse: sourceWarehouse,
    target_warehouse: targetWarehouse,
    transfer_mode: options.transferMode || __ENV.TRANSFER_MODE || 'direct',
    items: buildLines({
      distribution: true,
      contention: options.contention,
      includeRates: false,
      warehouse: sourceWarehouse,
      lineCount: options.lineCount,
    }),
    printer_type: options.printerType || __ENV.PRINTER_TYPE || 'voucher',
  };
  if (options.outgoingStockEntry) {
    payload.outgoing_stock_entry = options.outgoingStockEntry;
  }
  return payload;
}

let localDocSequence = 0;

function nextLocalName(prefix) {
  localDocSequence += 1;
  return `${prefix}-${localDocSequence}`;
}

function salesInvoiceDocument(options = {}) {
  const warehouse = options.warehouse || randomItem(shopWarehouseSequence());
  const lines = buildLines({
    distribution: warehouseRole(warehouse) === 'distribution',
    contention: options.contention,
    warehouse,
    lineCount: options.lineCount,
  });
  const parentName = nextLocalName('new-sales-invoice');
  return {
    doctype: 'Sales Invoice',
    __islocal: 1,
    docstatus: 0,
    name: parentName,
    customer: options.customer || selectedCustomer(),
    company: manifestContext.company,
    posting_date: today(),
    due_date: today(),
    set_posting_time: 1,
    update_stock: 1,
    set_warehouse: warehouse,
    selling_price_list: options.priceList || selectedPriceList(),
    currency: __ENV.CURRENCY || manifestContext.defaultCurrency,
    conversion_rate: 1,
    is_pos: 1,
    payments: [
      {
        doctype: 'Sales Invoice Payment',
        __islocal: 1,
        docstatus: 0,
        name: `${parentName}-payment-1`,
        parent: parentName,
        parenttype: 'Sales Invoice',
        parentfield: 'payments',
        idx: 1,
        mode_of_payment: options.paymentMode || __ENV.PAYMENT_MODE || 'Cash',
        account: manifestContext.defaultCashAccount || '',
        amount: 0,
      },
    ],
    items: lines.map((line, index) => ({
      doctype: 'Sales Invoice Item',
      __islocal: 1,
      docstatus: 0,
      name: `${parentName}-item-${index + 1}`,
      parent: parentName,
      parenttype: 'Sales Invoice',
      parentfield: 'items',
      idx: index + 1,
      item_code: line.item_code,
      qty: line.qty,
      uom: line.uom,
      warehouse,
      rate: line.rate || 0,
    })),
  };
}

function stockEntryDocument(options = {}) {
  const sourceWarehouse = options.sourceWarehouse || randomItem(shopWarehouseSequence());
  const targetWarehouse = options.targetWarehouse || chooseTransferTarget(sourceWarehouse);
  const lines = buildLines({
    distribution: true,
    contention: options.contention,
    includeRates: false,
    warehouse: sourceWarehouse,
    lineCount: options.lineCount,
  });
  const parentName = nextLocalName('new-stock-entry');
  return {
    doctype: 'Stock Entry',
    __islocal: 1,
    docstatus: 0,
    name: parentName,
    stock_entry_type: 'Material Transfer',
    purpose: 'Material Transfer',
    company: manifestContext.company,
    posting_date: today(),
    set_posting_time: 1,
    from_warehouse: sourceWarehouse,
    to_warehouse: targetWarehouse,
    items: lines.map((line, index) => ({
      doctype: 'Stock Entry Detail',
      __islocal: 1,
      docstatus: 0,
      name: `${parentName}-detail-${index + 1}`,
      parent: parentName,
      parenttype: 'Stock Entry',
      parentfield: 'items',
      idx: index + 1,
      item_code: line.item_code,
      qty: line.qty,
      uom: line.uom,
      s_warehouse: sourceWarehouse,
      t_warehouse: targetWarehouse,
    })),
  };
}

// Pre-calls intentionally carry only method + args. The executor in workflow.js
// serializes the current (server-evolved) document for each row prep call so
// run_doc_method always sees the state returned by the previous call.
function runDocMethodPreCalls(doc, methodName) {
  return (doc.items || []).map((line, index) => {
    const args =
      methodName === 'process_item_selection'
        ? {
            item_idx: index + 1,
            reset_item_details: false,
            parentfield: 'items',
          }
        : {
            item_code: line.item_code,
            qty: line.qty,
            uom: line.uom,
            s_warehouse: line.s_warehouse || '',
            t_warehouse: line.t_warehouse || '',
            warehouse: line.warehouse || line.s_warehouse || '',
            company: doc.company,
            posting_date: doc.posting_date,
          };
    return {
      method: methodName,
      index,
      args,
    };
  });
}

export function buildStockSaleFlow(options = {}) {
  const doc = salesInvoiceDocument(options);
  const preCalls = runDocMethodPreCalls(doc, 'process_item_selection');
  return {
    doc,
    preCalls,
    savedocs: {
      doc: JSON.stringify(doc),
      action: 'Submit',
    },
    roundTrips: preCalls.length + 1,
  };
}

export function buildStockTransferFlow(options = {}) {
  const doc = stockEntryDocument(options);
  const preCalls = runDocMethodPreCalls(doc, 'get_item_details');
  return {
    doc,
    preCalls,
    savedocs: {
      doc: JSON.stringify(doc),
      action: 'Submit',
    },
    roundTrips: preCalls.length + 1,
  };
}

export function manifestSummary() {
  return {
    items: manifestContext.items.length,
    warehouses: manifestContext.warehouses.length,
    priceLists: manifestContext.priceLists.length,
    customers: manifestContext.customers.length,
    company: manifestContext.company,
  };
}
