"""Plane 3 — multi-window, multi-burn-rate SLO detection.

Static thresholds (``if error_rate > 0.05``) fail in two directions at once: they page on
a harmless spike and stay silent through a slow bleed. Burn rate — how fast the error
budget is being consumed relative to the rate that would exhaust it exactly at the end of
the window — fixes both, and is the standard from the Google SRE workbook.

Each alert pairs a long window (does this represent real budget consumption?) with a short
one (is it still happening right now?). Both must be burning for the alert to fire; the
short window is what stops an alert from hanging around for an hour after the incident is
over.

**Stated demo compression:** the error budget is 6 hours rolling, not 30 days. A 30-day
budget cannot burn 2% inside a 90-second demo, and quietly substituting a static threshold
while calling it burn-rate detection would be the dishonest version. This is said out loud
in the writeup.

The detector is our own service polling Prometheus every 15s rather than Alertmanager.
Detection is the thing that makes the system autonomous; it should be code we own and can
show, not YAML inside someone else's binary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

Severity = Literal["fast_burn", "slow_burn"]

#: Rolling error-budget window. See the compression note above.
BUDGET_WINDOW = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class BurnRateAlert:
    slo: str
    severity: Severity
    burn_rate: float
    long_window_s: int
    short_window_s: int
    observed_at: datetime

    @property
    def budget_exhausted_in(self) -> timedelta:
        """How long the budget lasts at the current burn rate. Drives incident priority."""
        if self.burn_rate <= 0:
            return timedelta.max
        return BUDGET_WINDOW / self.burn_rate


@dataclass(frozen=True, slots=True)
class BurnRateRule:
    severity: Severity
    long_window_s: int
    short_window_s: int
    threshold: float  # multiples of the budget-consumption rate


#: Windows are compressed for demo tempo; the ratios are the workbook's.
#: Fast burn: 2% of a 6h budget, fires in ~45-75s. Slow burn: 5%, fires in ~10min.
DEFAULT_RULES = (
    BurnRateRule("fast_burn", long_window_s=300, short_window_s=30, threshold=14.4),
    BurnRateRule("slow_burn", long_window_s=1800, short_window_s=180, threshold=6.0),
)


def burn_rate(sli: float, objective: float) -> float:
    """Budget consumption rate as a multiple of the sustainable rate.

    ``sli`` is the good-event ratio over the window; ``objective`` is the target (0.99).
    A burn rate of 1.0 exhausts the budget exactly at the end of the budget window.
    """
    budget = 1.0 - objective
    if budget <= 0:
        return 0.0 if sli >= 1.0 else float("inf")
    return (1.0 - sli) / budget


def evaluate(
    slo: str,
    objective: float,
    long_sli: float,
    short_sli: float,
    *,
    rules: tuple[BurnRateRule, ...] = DEFAULT_RULES,
    now: datetime | None = None,
) -> BurnRateAlert | None:
    """Return the most severe firing alert, or None.

    Both windows must exceed the threshold. Rules are evaluated in declaration order, so
    the fast-burn rule wins when both fire — one incident, at its true severity.
    """
    now = now or datetime.now(timezone.utc)
    long_rate = burn_rate(long_sli, objective)
    short_rate = burn_rate(short_sli, objective)

    for rule in rules:
        if long_rate >= rule.threshold and short_rate >= rule.threshold:
            return BurnRateAlert(
                slo=slo,
                severity=rule.severity,
                burn_rate=long_rate,
                long_window_s=rule.long_window_s,
                short_window_s=rule.short_window_s,
                observed_at=now,
            )
    return None


def correlate(
    alerts: list[BurnRateAlert],
    *,
    window: timedelta = timedelta(minutes=2),
) -> list[list[BurnRateAlert]]:
    """Group alerts into incidents by temporal proximity.

    One fault fires many alerts — a saturated pool breaches availability at the gateway
    and latency at orders-svc within seconds of each other. Without correlation the engine
    opens two investigations into one root cause and pays twice to reach the same verdict.

    v1 correlates on time alone. Topology adjacency and signal similarity are the two
    obvious next axes; the ``Incident`` boundary is already the right seam for them.
    """
    if not alerts:
        return []

    ordered = sorted(alerts, key=lambda a: a.observed_at)
    clusters: list[list[BurnRateAlert]] = [[ordered[0]]]

    for alert in ordered[1:]:
        if alert.observed_at - clusters[-1][-1].observed_at <= window:
            clusters[-1].append(alert)
        else:
            clusters.append([alert])

    return clusters


def noise_reduction_ratio(alerts: list[BurnRateAlert]) -> float:
    """Alerts in, incidents out. A real number to report, not a claim to make."""
    if not alerts:
        return 0.0
    return 1.0 - (len(correlate(alerts)) / len(alerts))
