"""Prometheus metrics — the exact names in V1_SCOPE §C.3.

Metric names are a contract, not a detail. The fault catalog's ``expected_signals`` are
written against these strings, the SLO recording rules query them, and the eval scores
whether the agent found them. Renaming one silently breaks three things that will not fail
loudly, so they are declared once, here.

The ``cohort`` label on the HTTP metrics is what makes F-05 detectable at all: an N+1
regression that only affects large baskets is invisible in aggregate p99 and obvious the
moment you segment by basket size.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- RED metrics -----------------------------------------------------------------------

http_requests_total = Counter(
    "http_requests_total",
    "HTTP requests served",
    ["service", "route", "status", "cohort"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ["service", "route", "cohort"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 2.0, 4.0, 8.0),
)

# --- Upstream calls — the F-04 discriminator -------------------------------------------

upstream_requests_total = Counter(
    "upstream_requests_total",
    "Calls made to an upstream service",
    ["service", "upstream", "status"],
)

upstream_duration_seconds = Histogram(
    "upstream_duration_seconds",
    "Upstream call duration",
    ["service", "upstream"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0),
)

# --- Connection pool — the F-03 signal --------------------------------------------------

db_pool_size = Gauge("db_pool_size", "Configured maximum pool size", ["service"])
db_pool_in_use = Gauge("db_pool_in_use", "Connections currently checked out", ["service"])
db_pool_waiters = Gauge("db_pool_waiters", "Callers waiting for a connection", ["service"])

db_pool_acquire_seconds = Histogram(
    "db_pool_acquire_seconds",
    "Time spent waiting to acquire a pooled connection",
    ["service"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 3.0),
)

# --- Cache — the F-01 / F-02 signal ------------------------------------------------------

cache_ops_total = Counter(
    "cache_ops_total",
    "Cache operations",
    ["service", "op", "result"],  # result: hit | miss | error
)

# --- Queue — the freshness SLI and the F-06 signal ---------------------------------------

queue_depth = Gauge("queue_depth", "Jobs waiting in the queue", ["queue"])

job_oldest_age_seconds = Gauge(
    "job_oldest_age_seconds",
    "Age of the oldest unfinished job — the freshness SLI's leading indicator",
    ["queue"],
)

job_attempts_total = Counter(
    "job_attempts_total",
    "Job attempts",
    ["queue", "status"],  # status: complete | retry | dead_letter
)

orders_created_total = Counter(
    "orders_created_total",
    "Orders accepted — the freshness SLI's denominator",
)

order_completion_seconds = Histogram(
    "order_completion_seconds",
    "Time from order creation to COMPLETE — the freshness SLI",
    buckets=(1, 5, 10, 20, 30, 60, 120, 300, 600),
)


def observe_request(
    service: str, route: str, status: int, cohort: str, duration_s: float
) -> None:
    http_requests_total.labels(service, route, str(status), cohort or "none").inc()
    http_request_duration_seconds.labels(service, route, cohort or "none").observe(duration_s)


def observe_upstream(service: str, upstream: str, status: str, duration_s: float) -> None:
    upstream_requests_total.labels(service, upstream, status).inc()
    upstream_duration_seconds.labels(service, upstream).observe(duration_s)


def observe_cache(service: str, op: str, result: str) -> None:
    cache_ops_total.labels(service, op, result).inc()
