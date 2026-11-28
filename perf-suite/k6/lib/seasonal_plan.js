export const REGULAR_VOUCHER_MIX = {
  sale_small: { voucherType: 'sale', lineCount: 10, perMinute: 54 },
  sale_medium: { voucherType: 'sale', lineCount: 50, perMinute: 16 },
  sale_large: { voucherType: 'sale', lineCount: 200, perMinute: 4 },
  transfer: { voucherType: 'transfer', lineCount: 50, perMinute: 9 },
  purchase_cycle: { voucherType: 'purchase_cycle', lineCount: 50, perMinute: 5 },
};

export const CONTENTION_SALES_PER_BURST = 6;
export const CONTENTION_TRANSFERS_PER_BURST = 1;
export const BASE_SEARCHES_PER_MINUTE = 80;
const CONTENTION_SHOP_COUNT = 6;

export const VOUCHER_TYPES = ['sale', 'transfer', 'purchase_receipt', 'purchase_invoice'];

export function retryWitness(work) {
  return work.segment === 'baseline' && work.minute === 0 && work.sequence === 0 && !work.contention &&
    ['sale_small', 'transfer', 'purchase_receipt', 'purchase_invoice'].includes(work.mixKey);
}

export function stableHash32(value) {
  let hash = 2166136261;
  const text = String(value);
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

export function hashHex(value) {
  const first = stableHash32(value).toString(16).padStart(8, '0');
  const second = stableHash32(`second:${value}`).toString(16).padStart(8, '0');
  return `${first}${second}`;
}

export function hashUnit(value) {
  return stableHash32(value) / 4294967296;
}

function hashText(hash, value) {
  const text = String(value);
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function stableHash32Three(first, second, third) {
  return hashText(hashText(hashText(2166136261, first), second), third) >>> 0;
}

function hashUnitThree(first, second, third) {
  return stableHash32Three(first, second, third) / 4294967296;
}

export function mulberry32(seed) {
  let state = seed >>> 0;
  return function next() {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

export function parseDurationSeconds(value) {
  const text = String(value || '').trim();
  const match = text.match(/^(\d+(?:\.\d+)?)(ms|s|m|h)?$/i);
  if (!match) {
    throw new Error(`Invalid k6 duration '${value}'`);
  }
  const amount = Number(match[1]);
  const unit = (match[2] || 's').toLowerCase();
  const multiplier = unit === 'ms' ? 0.001 : unit === 's' ? 1 : unit === 'm' ? 60 : 3600;
  return amount * multiplier;
}

export function formatDurationSeconds(seconds) {
  if (seconds === 0) {
    return '0s';
  }
  if (seconds % 3600 === 0) {
    return `${seconds / 3600}h`;
  }
  if (seconds % 60 === 0) {
    return `${seconds / 60}m`;
  }
  return `${seconds}s`;
}

function gcd(a, b) {
  let left = Math.abs(a);
  let right = Math.abs(b);
  while (right) {
    const remainder = left % right;
    left = right;
    right = remainder;
  }
  return left || 1;
}

export function decimalToRatio(value) {
  const text = String(value);
  if (!/^\d+(?:\.\d+)?$/.test(text)) {
    throw new Error(`Invalid positive decimal ratio '${value}'`);
  }
  const decimals = text.includes('.') ? text.split('.')[1].length : 0;
  const denominator = 10 ** decimals;
  const numerator = Math.round(Number(text) * denominator);
  const divisor = gcd(numerator, denominator);
  if (numerator <= 0) {
    throw new Error(`Ratio must be positive, got '${value}'`);
  }
  return [numerator / divisor, denominator / divisor];
}

function envNumber(env, name, fallback) {
  const raw = env[name];
  if (raw === undefined || raw === null || raw === '') {
    return fallback;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${name} must be numeric, got '${raw}'`);
  }
  return parsed;
}

export function seasonalSettings(env) {
  const profile = String(env.SEASONAL_PROFILE || 'acceptance').trim().toLowerCase();
  const smoke = profile === 'smoke';
  if (!['acceptance', 'smoke'].includes(profile)) {
    throw new Error(`SEASONAL_PROFILE must be acceptance or smoke, got '${profile}'`);
  }
  const overloadText = env.SEASONAL_OVERLOAD_MULTIPLIERS || (smoke ? '1.5,2' : '1.5,2,2.5,3');
  const overloadMultipliers = overloadText
    .split(',')
    .map((part) => Number(part.trim()))
    .filter((value) => Number.isFinite(value) && value > 1);
  if (!overloadMultipliers.length) {
    throw new Error('SEASONAL_OVERLOAD_MULTIPLIERS must contain at least one multiplier greater than 1');
  }
  return {
    profile,
    runId: String(env.RUN_ID || 'local-run'),
    seed: String(env.SEASONAL_SEED || ''),
    baselineDuration: env.SEASONAL_BASELINE_DURATION || (smoke ? '2m' : '60m'),
    marginDuration: env.SEASONAL_MARGIN_DURATION || (smoke ? '1m' : '15m'),
    overloadStepDuration: env.SEASONAL_OVERLOAD_STEP_DURATION || (smoke ? '1m' : '4m'),
    recoveryDuration: env.SEASONAL_RECOVERY_DURATION || (smoke ? '30s' : '2m'),
    overloadMultipliers,
    maxRetries: Math.max(1, Math.floor(envNumber(env, 'SEASONAL_MAX_RETRIES', 3))),
    retryBackoffMs: Math.max(0, envNumber(env, 'SEASONAL_RETRY_BACKOFF_MS', 350)),
    purchaseRecoveryPolls: Math.max(1, Math.floor(envNumber(env, 'SEASONAL_PURCHASE_RECOVERY_POLLS', 3))),
    purchaseRecoveryPollIntervalMs: Math.max(0, envNumber(env, 'SEASONAL_PURCHASE_RECOVERY_POLL_INTERVAL_MS', 350)),
    detailTimeoutSeconds: Math.max(1, envNumber(env, 'SEASONAL_DETAIL_TIMEOUT_SECONDS', 30)),
    detailPageCount: Math.max(1, Math.floor(envNumber(env, 'SEASONAL_DETAIL_PAGE_COUNT', 2))),
    voucherPreAllocatedVus: Math.max(1, Math.floor(envNumber(env, 'SEASONAL_VOUCHER_PREALLOCATED_VUS', 4))),
    voucherMaxVus: Math.max(4, Math.floor(envNumber(env, 'SEASONAL_VOUCHER_MAX_VUS', 64))),
    searchPreAllocatedVus: Math.max(1, Math.floor(envNumber(env, 'SEASONAL_SEARCH_PREALLOCATED_VUS', 2))),
    searchMaxVus: Math.max(2, Math.floor(envNumber(env, 'SEASONAL_SEARCH_MAX_VUS', 16))),
    exportEnqueueMethod: env.SEASONAL_EXPORT_ENQUEUE_METHOD || 'khitmee_ops.profit_summary.export_service.enqueue_profit_csv_export',
    exportDownloadMethod: env.SEASONAL_EXPORT_DOWNLOAD_METHOD || 'khitmee_ops.profit_summary.export_service.download_profit_csv_export',
    enableManagers: String(env.SEASONAL_ENABLE_MANAGERS || '1').toLowerCase() !== '0',
    enableExport: String(env.SEASONAL_ENABLE_EXPORT || '1').toLowerCase() !== '0',
    summaryReportName: env.SEASONAL_SUMMARY_REPORT || 'Fast Gross Profit',
    detailReportName: env.SEASONAL_DETAIL_REPORT || 'Fast Gross Profit',
    requireRetryEvidence: String(env.SEASONAL_REQUIRE_RETRY_EVIDENCE || '1').toLowerCase() !== '0',
    baselineOnly: String(env.SEASONAL_BASELINE_ONLY || '0').toLowerCase() === '1',
  };
}


const MANAGER_ACTOR_SPECS = [
  {
    key: 'summary_warehouse',
    envPrefix: 'SEASONAL_MANAGER_WAREHOUSE',
    authorization: 'read Fast Gross Profit grouped by Warehouse as an authorized non-administrator report reader',
  },
  {
    key: 'summary_monthly',
    envPrefix: 'SEASONAL_MANAGER_MONTHLY',
    authorization: 'read Fast Gross Profit grouped by Monthly as an authorized non-administrator report reader',
  },
  {
    key: 'detail_reader',
    envPrefix: 'SEASONAL_MANAGER_DETAIL',
    authorization: 'read the invoice-detail report as an authorized non-administrator report reader',
  },
  {
    key: 'export_requester',
    envPrefix: 'SEASONAL_MANAGER_EXPORT',
    authorization: 'enqueue and download the full-year Sales Invoice CSV export as an authorized non-administrator export requester',
  },
];

function credentialPair(env, userNames, passwordNames, label) {
  const user = userNames.map((name) => env[name]).find((value) => value !== undefined && value !== null && String(value).trim() !== '');
  const password = passwordNames.map((name) => env[name]).find((value) => value !== undefined && value !== null && String(value) !== '');
  if (!user) {
    throw new Error(`${label} user is required; set ${userNames.join(' or ')}. The seasonal benchmark does not use a hard-coded login.`);
  }
  if (!password) {
    throw new Error(`${label} password is required; set ${passwordNames.join(' or ')}.`);
  }
  return { user: String(user).trim(), password: String(password) };
}

export function seasonalActors(env, settings) {
  const cashier = credentialPair(env, ['PERF_USER', 'FRAPPE_USER'], ['PERF_PASSWORD', 'FRAPPE_PASSWORD'], 'cashier');
  const actors = {
    cashier: {
      actor: 'cashier',
      user: cashier.user,
      password: cashier.password,
      authorization: 'submit counter sales/transfers and purchase documents as an authorized non-administrator cashier',
    },
  };
  if (!settings.enableManagers) {
    if (actors.cashier.user.toLowerCase() === 'administrator') {
      throw new Error('Seasonal cashier discovery must use an authorized cashier user, not Administrator.');
    }
    return actors;
  }
  MANAGER_ACTOR_SPECS.forEach((spec) => {
    const pair = credentialPair(
      env,
      [`${spec.envPrefix}_USER`],
      [`${spec.envPrefix}_PASSWORD`],
      `${spec.key} (${spec.envPrefix})`
    );
    actors[spec.key] = {
      actor: spec.key,
      user: pair.user,
      password: pair.password,
      authorization: spec.authorization,
    };
  });
  const users = Object.values(actors).map((actor) => actor.user);
  const normalized = users.map((user) => user.toLowerCase());
  if (new Set(normalized).size !== normalized.length) {
    throw new Error(`Seasonal actors must be exact distinct users; got duplicate users among cashier/summary/detail/export actors: ${users.join(', ')}`);
  }
  if (normalized.includes('administrator')) {
    throw new Error('Seasonal actors must be authorized role users, not Administrator; administrator login would bypass the permission evidence being measured.');
  }
  return actors;
}

export function publicActors(actors) {
  const result = {};
  Object.keys(actors).sort().forEach((key) => {
    result[key] = {
      actor: actors[key].actor,
      user: actors[key].user,
      authorization: actors[key].authorization,
    };
  });
  return result;
}

function validIsoDate(value) {
  const text = String(value || '');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return false;
  }
  const parsed = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === text;
}

function dateOnly(isoValue) {
  return String(isoValue || '').slice(0, 10);
}

export function seasonalReportWindows(env, context) {
  const seeded = context.reportWindow || {};
  const asOf = dateOnly(env.REPORT_TO_DATE || seeded.final_voucher_date || seeded.to_date);
  const ytdFrom = dateOnly(env.REPORT_FROM_DATE || (asOf ? `${asOf.slice(0, 4)}-01-01` : ''));
  const fullFrom = dateOnly(env.EXPORT_FROM_DATE || seeded.from_date);
  const windows = {
    year_to_date: {
      scope: 'year_to_date',
      from_date: ytdFrom,
      to_date: asOf,
    },
    full_year: {
      scope: 'full_year',
      from_date: fullFrom,
      to_date: asOf,
    },
  };
  Object.values(windows).forEach((window) => {
    if (!validIsoDate(window.from_date) || !validIsoDate(window.to_date)) {
      throw new Error(`Seasonal ${window.scope} report window must use explicit YYYY-MM-DD dates; got ${window.from_date}..${window.to_date}`);
    }
    if (window.from_date > window.to_date) {
      throw new Error(`Seasonal ${window.scope} report window is reversed: ${window.from_date}..${window.to_date}`);
    }
  });
  const fullDays = (Date.parse(`${windows.full_year.to_date}T00:00:00Z`) - Date.parse(`${windows.full_year.from_date}T00:00:00Z`)) / 86400000;
  if (fullDays < 364) {
    throw new Error(`Seasonal full-year export window must span at least 365 inclusive dates; got ${windows.full_year.from_date}..${windows.full_year.to_date}`);
  }
  return windows;
}

export function buildSegments(settings) {
  const segments = [];
  let cursor = 0;
  function add(name, phase, durationText, scaleNum, scaleDen, overloadStep) {
    const durationSeconds = parseDurationSeconds(durationText);
    if (durationSeconds <= 0) {
      throw new Error(`${name} duration must be positive`);
    }
    segments.push({
      name,
      phase,
      overloadStep: overloadStep || 0,
      scaleNum,
      scaleDen,
      startSeconds: cursor,
      durationSeconds,
      duration: formatDurationSeconds(durationSeconds),
    });
    cursor += durationSeconds;
  }
  add('baseline', 'baseline', settings.baselineDuration, 1, 1, 0);
  if (!settings.baselineOnly) {
    add('margin', 'margin', settings.marginDuration, 5, 4, 0);
    settings.overloadMultipliers.forEach((multiplier, index) => {
      const [num, den] = decimalToRatio(multiplier);
      add(`overload_${index + 1}`, 'overload', settings.overloadStepDuration, num, den, multiplier);
    });
    add('recovery', 'recovery', settings.recoveryDuration, 1, 1, 0);
  }
  return { segments, totalDurationSeconds: cursor };
}

function allocatedMinuteCounts(perMinute, scaleNum, scaleDen) {
  const counts = [];
  for (let minute = 0; minute < scaleDen; minute += 1) {
    const end = Math.floor(((minute + 1) * perMinute * scaleNum) / scaleDen);
    const start = Math.floor((minute * perMinute * scaleNum) / scaleDen);
    counts.push(end - start);
  }
  return counts;
}

function shuffled(values, seedText) {
  const random = mulberry32(stableHash32(seedText));
  const copy = values.slice();
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const other = Math.floor(random() * (index + 1));
    const value = copy[index];
    copy[index] = copy[other];
    copy[other] = value;
  }
  return copy;
}

export function coordinatedMinuteAllocations(seed, segment) {
  const mixKeys = Object.keys(REGULAR_VOUCHER_MIX);
  const previous = {};
  mixKeys.forEach((mixKey) => {
    previous[mixKey] = 0;
  });
  const rows = [];
  for (let minute = 0; minute < segment.scaleDen; minute += 1) {
    const step = minute + 1;
    const cumulativeRowTotal = Math.floor((88 * segment.scaleNum * step) / segment.scaleDen);
    const current = {};
    const candidates = [];
    mixKeys.forEach((mixKey) => {
      const perMinute = REGULAR_VOUCHER_MIX[mixKey].perMinute;
      const numerator = perMinute * segment.scaleNum * step;
      const lower = Math.floor(numerator / segment.scaleDen);
      const upper = Math.ceil(numerator / segment.scaleDen);
      const initial = Math.max(previous[mixKey], lower);
      if (initial > upper) {
        throw new Error(`internal cumulative mix schedule error for ${segment.name}`);
      }
      current[mixKey] = initial;
      const capacity = upper - initial;
      if (capacity > 0) {
        candidates.push({
          mixKey,
          capacity,
          fraction: (numerator % segment.scaleDen) / segment.scaleDen,
          tie: hashUnit(`${seed}:${segment.name}:apportion:${minute}:${mixKey}`),
        });
      }
    });
    let needed = cumulativeRowTotal - mixKeys.reduce((total, mixKey) => total + current[mixKey], 0);
    if (needed < 0) {
      throw new Error(`internal cumulative mix row error for ${segment.name}`);
    }
    candidates.sort((left, right) => right.fraction - left.fraction || right.tie - left.tie);
    candidates.forEach((candidate) => {
      if (needed > 0 && candidate.capacity > 0) {
        current[candidate.mixKey] += 1;
        needed -= 1;
      }
    });
    if (needed !== 0) {
      throw new Error(`internal cumulative mix apportionment error for ${segment.name}`);
    }
    const allocation = {};
    mixKeys.forEach((mixKey) => {
      allocation[mixKey] = current[mixKey] - previous[mixKey];
      if (allocation[mixKey] < 0) {
        throw new Error(`internal cumulative mix monotonicity error for ${segment.name}`);
      }
      previous[mixKey] = current[mixKey];
    });
    rows.push(allocation);
  }
  return rows;
}

export function buildVoucherTemplate(seed, segment) {
  const allocations = coordinatedMinuteAllocations(seed, segment);
  const byMinute = allocations.map(() => []);
  allocations.forEach((counts, minute) => {
    Object.keys(REGULAR_VOUCHER_MIX).forEach((mixKey) => {
      const mix = REGULAR_VOUCHER_MIX[mixKey];
      const count = counts[mixKey] || 0;
      for (let sequence = 0; sequence < count; sequence += 1) {
        byMinute[minute].push({
          mixKey,
          type: mix.voucherType,
          lineCount: mix.lineCount,
          minute,
          sequence,
          phase: segment.phase,
          segment: segment.name,
          overloadStep: segment.overloadStep,
        });
      }
    });
  });
  const template = [];
  byMinute.forEach((minuteEntries, minute) => {
    shuffled(minuteEntries, `${seed}:${segment.name}:minute:${minute}`).forEach((entry) => template.push(entry));
  });
  return template;
}

export function scaledArrivalRate(iterationsPerMinute, segment) {
  return {
    rate: iterationsPerMinute * segment.scaleNum,
    timeUnit: `${segment.scaleDen}m`,
  };
}


export function streamIterationBounds(durationSeconds, iterationsPerMinute, segment) {
  const nominal = (durationSeconds / 60) * iterationsPerMinute * (segment.scaleNum / segment.scaleDen);
  const minimum = Math.max(1, Math.ceil(nominal - 1e-9));
  const endpointAligned = Math.abs(nominal - Math.round(nominal)) < 1e-9;
  return {
    nominal,
    minimum,
    maximum: minimum + (endpointAligned ? 1 : 0),
    endpoint_aligned: endpointAligned,
  };
}

export function buildContentionTemplate(segment) {
  const iterationsPerBlock = Math.max(1, segment.scaleNum);
  const template = [];
  const counts = allocatedMinuteCounts(1, segment.scaleNum, segment.scaleDen);
  for (let iteration = 0; iteration < iterationsPerBlock; iteration += 1) {
    let cumulative = 0;
    for (let minute = 0; minute < segment.scaleDen; minute += 1) {
      const count = counts[minute];
      if (iteration < cumulative + count) {
        template.push({ minute, sequence: iteration - cumulative });
        break;
      }
      cumulative += count;
    }
  }
  if (template.length !== iterationsPerBlock) {
    throw new Error(`internal contention schedule error for ${segment.name}`);
  }
  return template;
}

export function seasonalWarehouses(context) {
  const warehouses = context.warehouses || [];
  const distribution = warehouses.filter((warehouse) => warehouse.role === 'distribution').map((warehouse) => warehouse.name);
  const retail = warehouses.filter((warehouse) => warehouse.role !== 'distribution').map((warehouse) => warehouse.name);
  return {
    all: distribution.concat(retail),
    distribution,
    retail,
  };
}

export function validateSeasonalManifest(context) {
  const errors = [];
  const warehouses = seasonalWarehouses(context);
  if ((context.warehouses || []).length !== 6 || warehouses.distribution.length < 2 || warehouses.retail.length < 4) {
    errors.push('seed manifest must contain exactly six warehouses including at least two distribution hubs and four retail shops');
  }
  if (!(context.items || []).length) {
    errors.push('seed manifest contains no item catalog');
  }
  if (!(context.suppliers || []).length) {
    errors.push('seed manifest contains no suppliers for purchase receipts/invoices');
  }
  if (!context.reportWindow || !context.reportWindow.from_date || !context.reportWindow.to_date) {
    errors.push('seed manifest report_window must contain from_date and to_date for year-to-date/full-year evidence');
  }
  (context.warehouses || []).forEach((warehouse) => {
    const entry = context.inStock[String(warehouse.name || '').toLowerCase()];
    const count = entry ? (entry.count !== undefined ? entry.count : entry.items ? entry.items.length : 0) : 0;
    const required = warehouse.role === 'distribution' ? 200 : 50;
    if (count < required) {
      errors.push(`${warehouse.name} has ${count} distinct seeded in-stock items; seasonal baskets require at least ${required}`);
    }
  });
  return errors;
}

function stockEntry(context, warehouse) {
  return context.inStock[String(warehouse || '').toLowerCase()] || { count: 0, item_codes: [], quantities: [], uoms: [] };
}

function stockRowsFor(context, warehouse) {
  const entry = context.inStock[String(warehouse || '').toLowerCase()];
  if (entry && entry.items) {
    return entry.items;
  }
  const rows = [];
  for (let index = 0; index < (entry ? entry.count || 0 : 0); index += 1) {
    rows.push({ item_code: entry.item_codes[index], qty: entry.quantities[index], uom: entry.uoms[index] });
  }
  return rows;
}

function deterministicQty(seedKey, itemCode, distribution, availableQty) {
  const base = distribution ? 2 + (stableHash32Three(seedKey, ':qty:', itemCode) % 7) : 1 + (stableHash32Three(seedKey, ':qty:', itemCode) % 3);
  if (!Number.isFinite(Number(availableQty))) {
    return base;
  }
  const perVuCap = Math.max(1, Math.floor(Number(availableQty) / 8));
  return Math.max(1, Math.min(base, perVuCap));
}

function catalogRate(context, itemCode) {
  if (!context.catalogCodes) {
    const found = (context.items || []).find((candidate) => candidate.item_code === itemCode);
    return found && found.rate !== undefined ? { found: true, rate: found.rate } : { found: false };
  }
  let low = 0;
  let high = context.catalogCodes.length - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    const code = context.catalogCodes[mid];
    if (code === itemCode) {
      return context.catalogHasRate[mid] ? { found: true, rate: context.catalogRates[mid] } : { found: false };
    }
    if (code < itemCode) {
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return { found: false };
}

export function deterministicLines(context, warehouse, count, seedKey, options = {}) {
  const entry = stockEntry(context, warehouse);
  const rowCount = entry.count !== undefined ? entry.count : entry.items ? entry.items.length : 0;
  if (rowCount < count) {
    throw new Error(`${warehouse} has only ${rowCount} distinct seeded in-stock items; cannot build ${count} distinct lines`);
  }
  const distribution = options.distribution !== undefined ? Boolean(options.distribution) : String(warehouse).toLowerCase().includes('dist');
  const scores = new Float64Array(rowCount);
  const order = new Uint32Array(rowCount);
  for (let index = 0; index < rowCount; index += 1) {
    const itemCode = entry.item_codes ? entry.item_codes[index] : entry.items[index].item_code;
    scores[index] = hashUnitThree(seedKey, ':pick:', itemCode) * Math.pow(index + 1, 0.55);
    order[index] = index;
  }
  order.sort((left, right) => scores[left] - scores[right] || left - right);
  const selectedIndexes = [];
  for (let index = 0; index < count; index += 1) {
    selectedIndexes.push(order[index]);
  }
  if (options.requiredItemCode) {
    let requiredIndex = -1;
    for (let index = 0; index < rowCount; index += 1) {
      const itemCode = entry.item_codes ? entry.item_codes[index] : entry.items[index].item_code;
      if (itemCode === options.requiredItemCode) {
        requiredIndex = index;
        break;
      }
    }
    if (requiredIndex < 0) {
      throw new Error(`${warehouse} does not stock required contention item ${options.requiredItemCode}`);
    }
    if (!selectedIndexes.includes(requiredIndex)) {
      selectedIndexes[selectedIndexes.length - 1] = requiredIndex;
    }
  }
  const byCode = {};
  const lines = [];
  selectedIndexes.forEach((rowIndex) => {
    const itemCode = entry.item_codes ? entry.item_codes[rowIndex] : entry.items[rowIndex].item_code;
    const qtyAvailable = entry.item_codes ? entry.quantities[rowIndex] : entry.items[rowIndex].qty;
    const uom = entry.item_codes ? entry.uoms[rowIndex] : entry.items[rowIndex].uom;
    if (byCode[itemCode]) {
      throw new Error(`duplicate item ${itemCode} selected for ${warehouse}`);
    }
    byCode[itemCode] = true;
    const line = {
      item_code: itemCode,
      qty: deterministicQty(seedKey, itemCode, distribution, qtyAvailable),
      uom: uom || context.defaultUom || 'Nos',
    };
    if (options.includeRates !== false) {
      const rate = catalogRate(context, itemCode);
      if (rate.found) {
        line.rate = rate.rate;
      }
    }
    lines.push(line);
  });
  lines.sort((a, b) => String(a.item_code).localeCompare(String(b.item_code)));
  if (lines.length !== count || new Set(lines.map((line) => line.item_code)).size !== count) {
    throw new Error(`internal basket error for ${warehouse}: expected ${count} distinct lines`);
  }
  return lines;
}

export function basketHash(lines) {
  return hashHex(lines.map((line) => `${line.item_code}:${line.qty}:${line.uom}`).join('|'));
}

export function seasonalRunPrefix(settings) {
  const seedPart = hashHex(`${settings.runId}:${settings.seed}`).slice(0, 10);
  return `K6S-${seedPart}-`;
}


export function contentionSaleSequence(sequence, shopIndex) {
  return sequence * CONTENTION_SHOP_COUNT + shopIndex;
}

export function contentionTransferSequence(sequence) {
  // A contention burst's sale fan-out occupies sequence*6..sequence*6+5. The
  // transfer is a separate mix key, but preserving the burst sequence keeps its
  // client voucher id unique across multiple fractional-minute bursts in the
  // same logical minute. Resetting it to zero was the duplicate-start defect.
  return sequence;
}

export function makeClientVoucherId(settings, work) {
  const raw = `${seasonalRunPrefix(settings)}${work.segment}-${work.mixKey}-m${work.minute}-s${work.sequence}`;
  return raw.replace(/[^A-Za-z0-9_.:-]/g, '-').slice(0, 140);
}

export function buildSeasonalPlan(env, context) {
  const settings = seasonalSettings(env);
  if (!settings.seed) {
    settings.seed = String((context.raw && context.raw.seed_id) || 'seedless-manifest');
  }
  const manifestErrors = validateSeasonalManifest(context);
  if (manifestErrors.length) {
    throw new Error(`Seasonal benchmark seed manifest is insufficient: ${manifestErrors.join('; ')}`);
  }
  const built = buildSegments(settings);
  const segments = built.segments.map((segment) => {
    const template = buildVoucherTemplate(settings.seed, segment);
    const regularRate = scaledArrivalRate(88, segment);
    const contentionRate = scaledArrivalRate(1, segment);
    const searchRate = scaledArrivalRate(BASE_SEARCHES_PER_MINUTE, segment);
    return Object.assign({}, segment, {
      template,
      contentionTemplate: buildContentionTemplate(segment),
      regularRate,
      contentionRate,
      searchRate,
      arrivalBounds: {
        regular_iterations: streamIterationBounds(segment.durationSeconds, 88, segment),
        contention_iterations: streamIterationBounds(segment.durationSeconds, 1, segment),
        search_iterations: streamIterationBounds(segment.durationSeconds, BASE_SEARCHES_PER_MINUTE, segment),
      },
    });
  });
  const actors = seasonalActors(env, settings);
  const reportWindows = seasonalReportWindows(env, context);
  return {
    settings,
    segments,
    totalDurationSeconds: built.totalDurationSeconds,
    warehouses: seasonalWarehouses(context),
    runPrefix: seasonalRunPrefix(settings),
    actors,
    reportWindows,
  };
}

export function publicPlanConfig(plan) {
  return {
    profile: plan.settings.profile,
    run_id: plan.settings.runId,
    seed: plan.settings.seed,
    run_prefix: plan.runPrefix,
    managers_enabled: plan.settings.enableManagers,
    export_enabled: plan.settings.enableExport,
    actors: publicActors(plan.actors),
    report_windows: plan.reportWindows,
    total_duration_seconds: plan.totalDurationSeconds,
    phases: plan.segments.map((segment) => ({
      name: segment.name,
      phase: segment.phase,
      overload_step: segment.overloadStep,
      start_seconds: segment.startSeconds,
      duration_seconds: segment.durationSeconds,
      scale: segment.scaleNum / segment.scaleDen,
      scale_num: segment.scaleNum,
      scale_den: segment.scaleDen,
      regular_rate: segment.regularRate,
      contention_rate: segment.contentionRate,
      search_rate: segment.searchRate,
      template_iterations_per_block: segment.template.length,
      arrival_bounds: segment.arrivalBounds,
      voucher_template: segment.template.map((entry) => [entry.minute, entry.mixKey]),
      contention_template: segment.contentionTemplate,
    })),
  };
}
