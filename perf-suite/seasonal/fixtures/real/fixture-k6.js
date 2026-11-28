import http from 'k6/http';
import exec from 'k6/execution';

const endpointClasses = ['cashier_submit', 'search', 'login/bootstrap', 'manager_summary', 'manager_detail', 'manager_export'];
const thresholds = {};
endpointClasses.forEach((endpointClass) => {
  thresholds[`http_reqs{endpoint_class:${endpointClass}}`] = ['count>=0'];
});

export const options = {
  vus: 1,
  iterations: endpointClasses.length,
  thresholds,
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
  const endpointClass = endpointClasses[iteration % endpointClasses.length];
  const response = http.get('http://fixture-http:8080/', {
    headers: {
      'X-K6-Request-Id': `fixture-run-vu1-${iteration}`,
      'X-K6-Endpoint-Class': endpointClass,
    },
    tags: { endpoint_class: endpointClass, name: 'fixture_request' },
  });
  marker({ event: response.status === 200 ? 'search_complete' : 'search_reject', phase: 'baseline', endpoint_class: endpointClass, duration_ms: response.timings.duration });
}

export function teardown() {
  marker({ event: 'measured_window_end' });
}
