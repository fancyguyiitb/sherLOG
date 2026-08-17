"""Burn-rate detection and alert correlation — Plane 3.

"Autonomous" has to mean something observable, not "we call it on a cron". These tests pin
down what fires, what does not, and why one fault produces one incident rather than four.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sherlog.detect.burn_rate import (
    BurnRateAlert,
    burn_rate,
    correlate,
    evaluate,
    noise_reduction_ratio,
)

T0 = datetime(2026, 8, 15, 14, 30, tzinfo=timezone.utc)


def test_a_perfect_sli_burns_nothing():
    assert burn_rate(sli=1.0, objective=0.99) == 0.0


def test_burn_rate_one_exhausts_the_budget_exactly():
    """1% errors against a 99% objective is exactly the sustainable rate."""
    assert burn_rate(sli=0.99, objective=0.99) == pytest.approx(1.0)


def test_fast_burn_fires_on_a_severe_breach():
    """15% of checkouts failing is 15x the sustainable rate — the F-03 shape."""
    alert = evaluate("availability", 0.99, long_sli=0.85, short_sli=0.85, now=T0)
    assert alert is not None
    assert alert.severity == "fast_burn"
    assert alert.budget_exhausted_in < timedelta(hours=1)


def test_slow_burn_fires_on_a_moderate_breach():
    """7% failing clears the 6x slow-burn threshold but not the 14.4x fast one — a bleed
    that deserves a ticket rather than a page."""
    alert = evaluate("latency", 0.99, long_sli=0.93, short_sli=0.93, now=T0)
    assert alert is not None
    assert alert.severity == "slow_burn"


def test_a_recovered_short_window_stops_the_alert():
    """Both windows must be burning.

    The long window still remembers the incident; the short window says it is over. Without
    the short-window requirement the alert would hang around for an hour after recovery,
    which is how teams learn to ignore alerts.
    """
    assert evaluate("availability", 0.99, long_sli=0.85, short_sli=1.0, now=T0) is None


def test_the_error_floor_does_not_fire():
    """The load profile declines 2% of payments on purpose.

    A detector that pages on the steady-state error floor is worse than useless. This is
    also why the floor exists: without it, any error at all would trivially be the incident
    and the agent would never have to reason about a delta.
    """
    assert evaluate("availability", 0.99, long_sli=0.98, short_sli=0.98, now=T0) is None


def test_fast_burn_wins_when_both_rules_match():
    """One incident, at its true severity — not two alerts for the same breach."""
    alert = evaluate("availability", 0.99, long_sli=0.50, short_sli=0.50, now=T0)
    assert alert is not None and alert.severity == "fast_burn"


# --- correlation -----------------------------------------------------------------------

def _alert(slo: str, offset_s: int) -> BurnRateAlert:
    return BurnRateAlert(slo, "fast_burn", 15.0, 300, 30, T0 + timedelta(seconds=offset_s))


def test_one_fault_produces_one_incident():
    """F-03 saturates the pool: availability breaches at the gateway and latency breaches
    at orders-svc within seconds. Four alerts, one root cause. Without correlation the
    engine opens four investigations and pays four times to reach the same verdict."""
    alerts = [
        _alert("availability", 0),
        _alert("latency", 8),
        _alert("availability", 15),
        _alert("latency", 22),
    ]
    assert len(correlate(alerts)) == 1


def test_genuinely_separate_incidents_stay_separate():
    alerts = [_alert("availability", 0), _alert("freshness", 3600)]
    assert len(correlate(alerts)) == 2


def test_noise_reduction_is_a_real_number():
    alerts = [_alert("availability", i * 10) for i in range(8)]
    assert noise_reduction_ratio(alerts) == pytest.approx(0.875)
