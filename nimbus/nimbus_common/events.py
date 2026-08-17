"""The log event enum.

``event`` is a stable dotted identifier, never an interpolated string. Variable data goes
in ``attrs``. Everything downstream depends on this:

*   Drain3 templating becomes near-perfect instead of merely good, because the invariant
    part of every line is genuinely invariant.
*   "This template has never been seen before" becomes a trustworthy signal rather than a
    noise generator, which is what F-06 turns on.

Adding an event means adding it here. ``nimbus_common.logging.log()`` rejects any event not
in this module, so the discipline is enforced at the call site rather than in review.
"""

from __future__ import annotations

# Request path
HTTP_REQUEST_START = "http.request.start"
HTTP_REQUEST_END = "http.request.end"
HTTP_REQUEST_ERROR = "http.request.error"

# Upstream calls
UPSTREAM_CALL = "upstream.call"
UPSTREAM_SLOW = "upstream.slow"
UPSTREAM_TIMEOUT = "upstream.timeout"
UPSTREAM_ERROR = "upstream.error"

# Database
DB_QUERY = "db.query"
DB_ACQUIRE = "db.acquire"
DB_ACQUIRE_TIMEOUT = "db.acquire.timeout"
DB_ERROR = "db.error"

# Cache
CACHE_HIT = "cache.hit"
CACHE_MISS = "cache.miss"
CACHE_ERROR = "cache.error"

# Domain
CHECKOUT_START = "checkout.start"
CHECKOUT_COMPLETE = "checkout.complete"
CHECKOUT_FAILED = "checkout.failed"
ORDER_CREATED = "order.created"
PAYMENT_CAPTURED = "payment.captured"
PAYMENT_DECLINED = "payment.declined"

# Worker / queue
JOB_CLAIMED = "job.claimed"
JOB_COMPLETE = "job.complete"
JOB_RETRY = "job.retry"
JOB_HANDLER_ERROR = "job.handler.error"
JOB_DEAD_LETTERED = "job.dead_lettered"

# Lifecycle
SERVICE_START = "service.start"
SERVICE_STOP = "service.stop"
CONFIG_LOADED = "config.loaded"

ALL_EVENTS: frozenset[str] = frozenset(
    value
    for name, value in list(globals().items())
    if name.isupper() and isinstance(value, str) and "." in value
)
