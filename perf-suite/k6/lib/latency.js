import { sleep } from 'k6';

export const SIMULATED_PING_MS = Number(__ENV.SIMULATED_PING_MS || 50);

export function simulatedPingMs(roundTrips = 1) {
  return Math.max(0, SIMULATED_PING_MS) * Math.max(0, roundTrips);
}

export function sleepForSimulatedRtt(roundTrips = 1) {
  const delayMs = simulatedPingMs(roundTrips);
  if (delayMs > 0) {
    sleep(delayMs / 1000);
  }
}

export function pureServerMs(endToEndMs, roundTrips = 1) {
  return Math.max(0, endToEndMs - simulatedPingMs(roundTrips));
}

export function measureCall(callable, options = {}) {
  const roundTrips = options.roundTrips === undefined ? 1 : options.roundTrips;
  const uxStart = Date.now();
  sleepForSimulatedRtt(roundTrips);
  const serverStart = Date.now();
  const value = callable();
  const serverDurationMs = Math.max(0, Date.now() - serverStart);
  const endToEndDurationMs = Math.max(0, Date.now() - uxStart);
  return {
    value,
    serverDurationMs,
    endToEndDurationMs,
    simulatedLatencyMs: simulatedPingMs(roundTrips),
    subtractedServerDurationMs: pureServerMs(endToEndDurationMs, roundTrips),
    roundTrips,
  };
}

export function jitterSeconds(baseSeconds, jitterRatio = 0.2) {
  const spread = Math.max(0, jitterRatio);
  return Math.max(0.1, baseSeconds * (1 - spread + Math.random() * spread * 2));
}
