import http from 'k6/http';

export const DEFAULT_TARGET_URL = 'http://127.0.0.1:18086';
export const DEFAULT_SITE_NAME = 'bench-loadtest.localhost';

function truthy(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value || '').toLowerCase());
}

export function splitBaseUrl(baseUrl) {
  const match = String(baseUrl || '').match(/^(https?:)\/\/([^/:?#]+)(?::(\d+))?([/?#].*)?$/i);
  if (!match) {
    throw new Error(`TARGET_URL must be an http(s) URL, got: ${baseUrl}`);
  }
  const protocol = match[1].toLowerCase();
  return {
    protocol,
    hostname: match[2].toLowerCase(),
    port: match[3] || (protocol === 'https:' ? '443' : '80'),
    suffix: match[4] || '',
  };
}

export function normalizeBaseUrl(baseUrl) {
  return String(baseUrl || DEFAULT_TARGET_URL).replace(/\/+$/, '');
}

export function isUnsafeTarget(baseUrl) {
  const parsed = splitBaseUrl(normalizeBaseUrl(baseUrl));
  const host = parsed.hostname;
  return parsed.port === '18085' || host === 'khitmee.localhost' || host.endsWith('.khitmee.localhost');
}

function isProtectedName(value) {
  const text = String(value || '').toLowerCase();
  const hostOnly = text.split(':')[0];
  return hostOnly === 'khitmee.localhost' || hostOnly.endsWith('.khitmee.localhost') || text.endsWith(':18085');
}

export function assertSafeTarget(baseUrl) {
  const normalized = normalizeBaseUrl(baseUrl);
  if (isUnsafeTarget(normalized)) {
    throw new Error(
      `Refusing to load-test the live development stack (${normalized}). ` +
        'Use the isolated khitmee-perf target on port 18086. There is no override for the protected site.'
    );
  }
  splitBaseUrl(normalized);
  return normalized;
}

export function configuredSiteName() {
  const siteName = __ENV.SITE_NAME || DEFAULT_SITE_NAME;
  if (isProtectedName(siteName)) {
    throw new Error(
      `Refusing to send X-Frappe-Site-Name for the live development stack (${siteName}). There is no override for the protected site.`
    );
  }
  return siteName;
}

export function baseHeaders(extra = {}) {
  const headers = Object.assign(
    {
      Accept: 'application/json',
      'X-Frappe-Site-Name': configuredSiteName(),
    },
    extra
  );
  const hostHeader = __ENV.HOST_HEADER !== undefined ? __ENV.HOST_HEADER : configuredSiteName();
  if (hostHeader) {
    if (isProtectedName(hostHeader)) {
      throw new Error(
        `Refusing to send a Host header for the live development stack (${hostHeader}). There is no override for the protected site.`
      );
    }
    headers.Host = hostHeader;
  }
  return headers;
}

function cookieValue(response, name) {
  const cookie = (response.cookies || {})[name];
  if (!cookie) {
    return '';
  }
  const first = Array.isArray(cookie) ? cookie[0] : cookie;
  return first && first.value ? first.value : '';
}

function parseJson(response) {
  try {
    return response.json();
  } catch (_err) {
    return {};
  }
}

function extractCsrfToken(html) {
  const patterns = [
    /frappe\.csrf_token\s*=\s*["']([^"']+)["']/,
    /window\.csrf_token\s*=\s*["']([^"']+)["']/,
    /["']csrf_token["']\s*:\s*["']([^"']+)["']/,
  ];
  for (let index = 0; index < patterns.length; index += 1) {
    const match = String(html || '').match(patterns[index]);
    if (match) {
      return match[1];
    }
  }
  return '';
}

function fetchCsrfToken(baseUrl, timeout) {
  const response = http.get(`${baseUrl}/app`, {
    headers: baseHeaders({ Accept: 'text/html,application/xhtml+xml' }),
    redirects: 5,
    timeout: timeout || '30s',
    tags: { name: 'desk_boot', endpoint: 'desk_boot' },
  });
  if (response.status >= 200 && response.status < 400) {
    return extractCsrfToken(response.body);
  }
  return '';
}

export function login(baseUrl, user, password, options = {}) {
  const normalized = assertSafeTarget(baseUrl);
  if (!user) {
    throw new Error('PERF_USER (or FRAPPE_USER) is required for the benchmark login.');
  }
  if (!password) {
    throw new Error('PERF_PASSWORD (or FRAPPE_PASSWORD) is required; no password is hard-coded or read from disk.');
  }

  const response = http.post(
    `${normalized}/api/method/login`,
    { usr: user, pwd: password },
    {
      headers: baseHeaders(),
      redirects: 0,
      timeout: options.timeout || '30s',
      tags: { name: 'login', endpoint: 'login' },
    }
  );

  if (response.status < 200 || response.status >= 400) {
    throw new Error(`Frappe login failed for ${user} with HTTP ${response.status}; credentials were not printed.`);
  }

  const body = parseJson(response);
  const sid = cookieValue(response, 'sid');
  if (!sid || sid === 'Guest') {
    throw new Error(
      `Frappe login for ${user} did not return an authenticated sid cookie (got ${sid ? 'Guest' : 'none'}). ` +
        'Refusing to continue with an anonymous or shared session.'
    );
  }
  const csrfToken =
    cookieValue(response, 'csrf_token') ||
    (body.message && body.message.csrf_token) ||
    body.csrf_token ||
    fetchCsrfToken(normalized, options.timeout);

  if (!csrfToken && !truthy(__ENV.ALLOW_MISSING_CSRF_TOKEN)) {
    throw new Error(
      'Frappe login succeeded, but no CSRF token could be read from /app. ' +
        'Set ALLOW_MISSING_CSRF_TOKEN=1 only when the benchmark site deliberately has ignore_csrf enabled.'
    );
  }

  return {
    baseUrl: normalized,
    user,
    sid,
    csrfToken,
    siteName: configuredSiteName(),
  };
}

export function authHeaders(session, extra = {}) {
  const headers = baseHeaders(extra);
  if (session && session.csrfToken) {
    headers['X-Frappe-CSRF-Token'] = session.csrfToken;
  }
  return headers;
}

export function postMethod(session, method, payload = {}, options = {}) {
  const headers = authHeaders(session, options.headers || {});
  let body = payload;
  if (!options.asForm) {
    body = JSON.stringify(payload);
    headers['Content-Type'] = 'application/json';
  }
  return http.post(`${session.baseUrl}/api/method/${method}`, body, {
    headers,
    redirects: 0,
    timeout: options.timeout || '60s',
    tags: options.tags || { name: method, endpoint: method },
  });
}

export function getMethod(session, methodAndQuery, options = {}) {
  return http.get(`${session.baseUrl}/api/method/${methodAndQuery}`, {
    headers: authHeaders(session, options.headers || {}),
    redirects: 0,
    timeout: options.timeout || '60s',
    tags: options.tags || { name: methodAndQuery.split('?')[0], endpoint: methodAndQuery.split('?')[0] },
  });
}
