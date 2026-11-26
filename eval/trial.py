"""Inject one fault and watch what the detector sees. A tuning tool, not part of the eval."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from faults.injector import mechanisms
from faults.schema import load_catalog
from sherlog.detect.service import _instant, healthy, poll

WATCH = [
    "sli:availability:ratio_rate30s", "sli:availability:ratio_rate5m",
    "sli:latency:ratio_rate30s", "sli:latency:ratio_rate5m",
    'sli:latency:ratio_rate5m:by_cohort{cohort="large"}',
    "sli:freshness:ratio_rate30s", "sli:freshness:ratio_rate5m",
    "discriminator:cache_miss_rate", "discriminator:redis_eviction_rate",
    "discriminator:psp_latency_p99", 'max(db_pool_waiters)',
    'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{service="gateway",route="POST /checkout"}[1m])))',
    'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{service="gateway",route="POST /checkout",cohort="large"}[1m])))',
    'sum(rate(http_requests_total{service="gateway",route="POST /checkout"}[1m]))',
]


def main() -> None:
    fault_id = sys.argv[1]
    decoys = "--decoys" in sys.argv
    catalog = load_catalog(Path("faults/catalog"))
    t0 = time.monotonic()
    while fault_id != "none" and not healthy():
        if time.monotonic() - t0 > 600:
            print("never became healthy", flush=True)
            return
        time.sleep(15)
    if fault_id != "none":
        fault = catalog[fault_id]
        rec, iid = mechanisms.inject(fault, [catalog[d] for d in fault.decoys] if decoys else [])
        print(f"injected {fault_id} at {rec.injected_at:%H:%M:%S} truth#{iid}", flush=True)
    start = time.monotonic()
    while time.monotonic() - start < float(sys.argv[2] if len(sys.argv) > 2 and sys.argv[2][0].isdigit() else 420):
        vals = []
        for e in WATCH:
            v = _instant(e)
            vals.append(f"{e.split(':')[1] if ':' in e else e}={'-' if v is None else f'{v:.3f}'}")
        alerts = poll()
        print(f"t+{time.monotonic() - start:4.0f}s " + " ".join(vals)
              + (f"  ALERTS={[(a.slo, a.severity, round(a.burn_rate, 1)) for a in alerts]}" if alerts else ""),
              flush=True)
        if alerts and "--stop" in sys.argv:
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
