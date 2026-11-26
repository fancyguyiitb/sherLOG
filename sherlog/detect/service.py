"""The autonomy loop: detector fires -> correlate -> open incident -> investigate.

Everything after "detector fires" runs with zero human input. The UI observes; it does not
drive. ``python -m sherlog.detect.service`` runs it forever; the eval harness calls
``wait_for_incident`` / ``open_incident`` directly so each scored run is the same code path
the demo uses.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import SETTINGS
from .burn_rate import DEFAULT_RULES, BurnRateAlert, correlate, evaluate

OBJECTIVE = 0.99
POLL_S = 15
#: Baseline checkout load is ~10-16 rps (diurnal); below this the load generator is not running.
MIN_HEALTHY_RPS = 5.0

#: (slo, long-window recording rule, short-window recording rule) per burn-rate rule.
SLIS: dict[str, dict[str, tuple[str, str]]] = {
    "availability": {"fast_burn": ("sli:availability:ratio_rate5m", "sli:availability:ratio_rate30s"),
                     "slow_burn": ("sli:availability:ratio_rate30m", "sli:availability:ratio_rate3m")},
    "latency": {"fast_burn": ("sli:latency:ratio_rate5m", "sli:latency:ratio_rate30s"),
                "slow_burn": ("sli:latency:ratio_rate30m", "sli:latency:ratio_rate3m")},
    "freshness": {"fast_burn": ("sli:freshness:ratio_rate5m", "sli:freshness:ratio_rate30s")},
}


class PrometheusUnavailable(RuntimeError):
    pass


def _instant(expr: str) -> float | None:
    """One instant query, retried. A transient stall is not an incident and must not crash
    an overnight eval; a persistent one raises PrometheusUnavailable."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = httpx.get(f"{SETTINGS.endpoints.prometheus}/api/v1/query",
                          params={"query": expr}, timeout=10).json()
            break
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    else:
        raise PrometheusUnavailable(f"prometheus query failed 3x: {last}")
    res = r.get("data", {}).get("result", [])
    if not res:
        return None
    v = float(res[0]["value"][1])
    return None if v != v else v  # NaN -> None


def poll() -> list[BurnRateAlert]:
    alerts = []
    now = datetime.now(timezone.utc)
    for slo, rules in SLIS.items():
        for rule in DEFAULT_RULES:
            if rule.severity not in rules:
                continue
            long_expr, short_expr = rules[rule.severity]
            long_sli, short_sli = _instant(long_expr), _instant(short_expr)
            if long_sli is None or short_sli is None:
                continue
            alert = evaluate(slo, OBJECTIVE, long_sli, short_sli, rules=(rule,), now=now)
            if alert:
                alerts.append(alert)
    return alerts


def healthy() -> bool:
    """No alert firing AND every 5-minute SLI back at objective — the precondition for
    injecting the next fault. "Not alerting" alone is too weak: a system recovering from
    the last run can sit below objective without crossing a burn threshold. Unreachable
    Prometheus is never healthy: nothing gets injected into a system we cannot see."""
    try:
        # No data is not health. With no traffic every SLI is 0/0 and "not below
        # objective"; a fault injected into a system with no load cannot breach anything.
        rps = _instant('sum(rate(http_requests_total{service="gateway",route="POST /checkout"}[1m]))')
        if rps is None or rps < MIN_HEALTHY_RPS:
            return False
        down = _instant('count(up{job="nimbus-services"} == 0)')
        if down:
            return False
        if poll():
            return False
        for slo in SLIS:
            v = _instant(f"sli:{slo}:ratio_rate5m")
            if v is None or v < OBJECTIVE:
                return False
    except PrometheusUnavailable:
        return False
    return True


def estimate_onset(slos: set[str], detected: datetime, lookback_s: int = 600) -> datetime:
    """When did the breach start? Walk back from detection along the 30s SLI of every
    alerting SLO until it was last at objective. Detection lags onset by however long the
    burn took to cross the long window — up to several minutes for a fault that hurts only
    a fifth of traffic — and the investigation window has to cover the onset, not just the
    alert."""
    start = detected.timestamp() - lookback_s
    onset = detected.timestamp()
    for slo in slos:
        r = httpx.get(f"{SETTINGS.endpoints.prometheus}/api/v1/query_range",
                      params={"query": f"sli:{slo}:ratio_rate30s", "start": start,
                              "end": detected.timestamp(), "step": 15}, timeout=10).json()
        res = r.get("data", {}).get("result", [])
        if not res:
            continue
        pts = [(float(t), float(v)) for t, v in res[0]["values"]]
        t_bad = None
        healthy_run = 0
        for t, v in reversed(pts):
            if v < OBJECTIVE:
                t_bad, healthy_run = t, 0
            else:
                healthy_run += 1
                if healthy_run >= 2 and t_bad is not None:  # tolerate one healthy blip
                    break
        if t_bad is not None:
            onset = min(onset, t_bad)
    return datetime.fromtimestamp(onset, timezone.utc)


def open_incident(alerts: list[BurnRateAlert]) -> dict[str, Any]:
    cluster = correlate(alerts)[0]
    detected = cluster[0].observed_at
    return {
        "id": "inc_" + uuid.uuid4().hex[:6],
        "detected_at": detected.isoformat(),
        "onset_at": estimate_onset({a.slo for a in cluster}, detected).isoformat(),
        "alerts": [{"slo": a.slo, "severity": a.severity, "burn_rate": round(a.burn_rate, 2)}
                   for a in cluster],
        "alert_count": len(alerts),
        "incident_count": len(correlate(alerts)),
    }


def wait_for_incident(timeout_s: float, poll_s: float = POLL_S) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            alerts = poll()
        except PrometheusUnavailable:
            alerts = []
        if alerts:
            return open_incident(alerts)
        time.sleep(poll_s)
    return None


def main() -> None:
    from ..engine.run import investigate
    from ..llm.client import OpenAIClient

    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="exit after one investigation")
    args = ap.parse_args()

    llm = OpenAIClient()
    print("detector polling SLO burn rates every 15s")
    while True:
        incident = wait_for_incident(timeout_s=10**9)
        assert incident
        print(f"incident {incident['id']} opened: {json.dumps(incident['alerts'])}")
        run = investigate(incident, llm)
        d = run.inv.decision
        print(f"  {run.id}: {d.outcome if d else '?'} — {run.report.get('root_cause', run.error)}")
        if args.once:
            return
        # Do not re-open on the same burn: wait until every SLO has recovered.
        while not healthy():
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
