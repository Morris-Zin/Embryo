import http from 'k6/http';
import exec from 'k6/execution';

export const options = {
  vus: 1,
  iterations: 4,
  thresholds: {
    'http_reqs{endpoint_class:search}': ['count>=0'],
  },
};

function marker(payload) {
  console.log(`KHITMEE_SEASONAL_EVENT ${JSON.stringify(Object.assign({ at: new Date().toISOString() }, payload))}`);
}

export function setup() {
  marker({ event: 'measured_window_start' });
  return {};
}

export default function () {
  const iteration = exec.scenario.iterationInTest;
  const response = http.get('http://fixture-http:8080/', {
    headers: {
      'X-K6-Request-Id': `fixture-run-vu1-${iteration}`,
      'X-K6-Endpoint-Class': 'search',
    },
    tags: { endpoint_class: 'search', name: 'fixture_search' },
  });
  marker({ event: response.status === 200 ? 'search_complete' : 'search_reject', phase: 'baseline', duration_ms: response.timings.duration });
}

export function teardown() {
  marker({ event: 'measured_window_end' });
}
