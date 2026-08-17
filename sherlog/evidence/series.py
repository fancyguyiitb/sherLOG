"""Metric shaping — the deterministic half of ``metric_query``.

A raw Prometheus range query at a 10s step over 30 minutes is 180 points per series, and
an investigation issues a dozen of them. Handing that to a model wastes most of the
context budget on digits it cannot use.

So every series is reduced to at most 120 points and paired with the same query shifted 24
hours, plus a computed delta. The model is asked to reason about *shape and change*, which
is the question, rather than about absolute values, which requires a baseline it does not
have.

This module is pure. The Prometheus HTTP call lives in the tool; the maths lives here so it
can be tested without a running backend, which is most of what ``frozen`` mode exercises.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

MAX_POINTS = 120

Point = tuple[datetime, float]


@dataclass(frozen=True, slots=True)
class SeriesSummary:
    """What the model actually sees for one metric."""

    labels: dict[str, str]
    points: tuple[Point, ...]
    baseline_points: tuple[Point, ...]
    current_p50: float
    current_p95: float
    baseline_p50: float
    delta_ratio: float  # current p95 / baseline p95 - 1

    def describe(self) -> str:
        direction = "up" if self.delta_ratio > 0 else "down"
        return (
            f"{self.labels} p50={self.current_p50:.4g} p95={self.current_p95:.4g} "
            f"(baseline p50={self.baseline_p50:.4g}, {direction} "
            f"{abs(self.delta_ratio) * 100:.0f}% vs 24h prior, "
            f"{len(self.points)} points)"
        )


def downsample(points: list[Point], max_points: int = MAX_POINTS) -> list[Point]:
    """Reduce to at most ``max_points`` by max-pooling within buckets.

    Max, not mean: this data exists to find latency spikes and saturation, and averaging is
    exactly the operation that hides them. A p99 excursion that survives downsampling is
    the point of downsampling at all.
    """
    if max_points <= 0:
        raise ValueError("max_points must be positive")
    if len(points) <= max_points:
        return list(points)

    bucket_size = len(points) / max_points
    out: list[Point] = []
    for i in range(max_points):
        lo = int(i * bucket_size)
        hi = max(lo + 1, int((i + 1) * bucket_size))
        bucket = points[lo:hi]
        if not bucket:
            continue
        out.append(max(bucket, key=lambda p: p[1]))
    return out


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def summarise(
    labels: dict[str, str],
    points: list[Point],
    baseline: list[Point],
    max_points: int = MAX_POINTS,
) -> SeriesSummary:
    """Downsample both series and compute the comparison the model is asked to reason over."""
    current = downsample(points, max_points)
    base = downsample(baseline, max_points)

    cur_values = [v for _, v in current]
    base_values = [v for _, v in base]

    cur_p95 = _quantile(cur_values, 0.95)
    base_p95 = _quantile(base_values, 0.95)
    delta = (cur_p95 / base_p95 - 1.0) if base_p95 else (0.0 if not cur_p95 else float("inf"))

    return SeriesSummary(
        labels=labels,
        points=tuple(current),
        baseline_points=tuple(base),
        current_p50=statistics.median(cur_values) if cur_values else 0.0,
        current_p95=cur_p95,
        baseline_p50=statistics.median(base_values) if base_values else 0.0,
        delta_ratio=delta,
    )
