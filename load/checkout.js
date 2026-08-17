// k6 traffic profile — runs continuously, never as a burst.
//
// Without steady baseline traffic there are no anomalies to detect and the metrics mean
// nothing. Three properties of this profile are load-bearing for the fault catalog:
//
//   1. COHORT SPLIT (80% small / 20% large baskets). Tagged here and carried as a header
//      onto every log line and metric label. This is the sole reason F-05 is detectable:
//      an N+1 regression that scales with basket size is invisible in aggregate p99 and
//      unmistakable once segmented.
//
//   2. DIURNAL SHAPE (0.6x to 1.6x over a 30-minute synthetic "day"). Baselines are
//      non-flat, so naive threshold detection visibly fails where burn-rate detection
//      works. A flat baseline would make the detector look better than it is.
//
//   3. ERROR FLOOR (2% invalid payment method). With a steady non-zero error rate, the
//      agent has to reason about a delta rather than treating any error at all as the
//      incident.

import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const TARGET = __ENV.K6_TARGET || 'http://gateway:8000';

// Baseline 10 rps checkout + 3 rps order reads = 13 rps.
// Sizing: 13 rps x ~6 log lines/request ~= 78 lines/s ~= 6.7M lines/day, roughly 1.5 GB raw
// before compression. Comfortable for Loki at 24h retention on the free-tier box.
export const options = {
  scenarios: {
    checkout: {
      executor: 'ramping-arrival-rate',
      startRate: 10,
      timeUnit: '1s',
      preAllocatedVUs: 40,
      maxVUs: 120,
      stages: diurnal(10),
    },
    reads: {
      executor: 'constant-arrival-rate',
      exec: 'readOrder',
      rate: 3,
      timeUnit: '1s',
      duration: '24h',
      preAllocatedVUs: 10,
    },
  },
  // Deliberately absent: pass/fail thresholds. k6 is the load generator, not the detector.
  // Detection belongs to the burn-rate service in Plane 3 — that is the thing being
  // demonstrated, and letting k6 decide what counts as an incident would hollow it out.
};

// A 30-minute synthetic day as a piecewise sine between 0.6x and 1.6x of baseline.
function diurnal(base) {
  const stages = [];
  const steps = 15;
  for (let i = 0; i < steps; i++) {
    const phase = (i / steps) * 2 * Math.PI;
    const multiplier = 1.1 + 0.5 * Math.sin(phase);
    stages.push({ duration: '2m', target: Math.round(base * multiplier) });
  }
  return stages;
}

const SKUS = Array.from({ length: 200 }, (_, i) => `SKU-${String(i).padStart(4, '0')}`);
const largeLatency = new Trend('checkout_latency_large');
const smallLatency = new Trend('checkout_latency_small');

function basket() {
  // 20% large. The split is what makes the cohort label informative; at 50/50 the
  // aggregate would move enough that segmenting adds nothing.
  const isLarge = Math.random() < 0.2;
  const count = isLarge
    ? 15 + Math.floor(Math.random() * 16)   // 15-30 items
    : 1 + Math.floor(Math.random() * 3);    // 1-3 items

  const items = [];
  for (let i = 0; i < count; i++) {
    items.push({ sku: SKUS[Math.floor(Math.random() * SKUS.length)], quantity: 1 });
  }
  return { items, cohort: isLarge ? 'large' : 'small' };
}

export default function () {
  const { items, cohort } = basket();

  // 2% of requests carry a payment method the psp always declines. This is the error
  // floor — a system whose steady-state error rate is exactly zero makes every fault
  // trivially detectable and teaches the agent nothing about deltas.
  const paymentMethod = Math.random() < 0.02 ? 'invalid-card' : 'card';

  const res = http.post(
    `${TARGET}/checkout`,
    JSON.stringify({ customer_id: `cust-${__VU}`, items, payment_method: paymentMethod }),
    {
      headers: {
        'Content-Type': 'application/json',
        'x-nimbus-cohort': cohort,   // carried through every hop
      },
      tags: { cohort },
    },
  );

  (cohort === 'large' ? largeLatency : smallLatency).add(res.timings.duration);

  check(res, {
    // 402 is a declined card, which is expected traffic, not a failure. Counting the
    // error floor as failed checks would put a permanent red number on the dashboard and
    // train everyone to ignore it.
    'checkout accepted': (r) => r.status === 200 || r.status === 402,
  });
}

// The read path. Under F-06 this keeps returning 200 with a stale PENDING forever: fast,
// correct, and wrong. Availability and latency stay green; only freshness notices.
export function readOrder() {
  const res = http.get(`${TARGET}/orders/${randomOrderId()}`, {
    headers: { 'x-nimbus-cohort': 'read' },
  });
  check(res, { 'order readable': (r) => r.status === 200 || r.status === 404 });
}

function randomOrderId() {
  return `00000000-0000-4000-8000-${String(Math.floor(Math.random() * 1e12)).padStart(12, '0')}`;
}
