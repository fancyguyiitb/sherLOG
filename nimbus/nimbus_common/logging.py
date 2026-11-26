"""Structured logging with a fixed schema, enforced at the call site.

Every line every service emits looks like this:

.. code-block:: json

    {"ts":"2026-08-15T14:32:07.412Z","level":"ERROR","service":"orders-svc",
     "commit_sha":"a3f9c21","config_hash":"7d21e0",
     "trace_id":"4bf92f...","span_id":"00f067aa...",
     "event":"db.acquire.timeout","route":"POST /orders","cohort":"large",
     "duration_ms":3021,"attrs":{"pool_size":3,"waiters":14},
     "msg":"connection acquisition timed out after 3000ms"}

Two rules everything downstream depends on:

1.  ``event`` is a stable dotted enum from ``nimbus_common.events``, never an interpolated
    string. Variable data lives in ``attrs``.
2.  ``commit_sha`` and ``config_hash`` appear on every single line. They are what let the
    agent correlate a log to a deploy or a config change without a join.

Loki labels are ``service`` and ``level`` only. Everything else stays in the JSON body and
is extracted at query time — high-cardinality labels are the standard way people destroy a
Loki install, and ``trace_id`` as a label would do it within a day.
"""

from __future__ import annotations

import json
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from . import events as ev
from .config import Settings

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "OFF": 100}

#: Per-request context, set by the middleware and read by every log call in that request.
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_span_id: ContextVar[str] = ContextVar("span_id", default="")
_route: ContextVar[str] = ContextVar("route", default="")
_cohort: ContextVar[str] = ContextVar("cohort", default="")


class UnknownEvent(ValueError):
    """Raised for an event not declared in ``nimbus_common.events``.

    Rejecting unknown events at the call site is what keeps the template store stable, and
    a stable template store is what makes ``novel: true`` mean something.
    """


class Logger:
    def __init__(self, settings: Settings, stream: Any = None) -> None:
        self.settings = settings
        self.stream = stream or sys.stdout
        self.threshold = _LEVELS.get(settings.log_level.upper(), 20)
        # TELEMETRY=off (the F-07 blind spot) silences this service's logs entirely.
        import os

        if os.environ.get("TELEMETRY", "on").lower() == "off":
            self.threshold = _LEVELS["OFF"]

    def log(
        self,
        level: str,
        event: str,
        msg: str = "",
        *,
        duration_ms: float | None = None,
        **attrs: Any,
    ) -> dict[str, Any] | None:
        if event not in ev.ALL_EVENTS:
            raise UnknownEvent(
                f"{event!r} is not declared in nimbus_common.events. Add it there rather "
                "than interpolating a new string; the template store depends on it."
            )
        if _LEVELS.get(level, 20) < self.threshold:
            return None

        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": level,
            "service": self.settings.service,
            "commit_sha": self.settings.commit_sha,
            "config_hash": self.settings.config_hash,
            "trace_id": _trace_id.get() or _otel_ids()[0],
            "span_id": _span_id.get() or _otel_ids()[1],
            "event": event,
            "route": _route.get(),
            "cohort": _cohort.get(),
        }
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 2)
        if attrs:
            record["attrs"] = attrs
        if msg:
            record["msg"] = msg

        print(json.dumps(record, default=str), file=self.stream, flush=True)
        return record

    def debug(self, event: str, msg: str = "", **kw: Any) -> dict[str, Any] | None:
        return self.log("DEBUG", event, msg, **kw)

    def info(self, event: str, msg: str = "", **kw: Any) -> dict[str, Any] | None:
        return self.log("INFO", event, msg, **kw)

    def warn(self, event: str, msg: str = "", **kw: Any) -> dict[str, Any] | None:
        return self.log("WARN", event, msg, **kw)

    def error(self, event: str, msg: str = "", **kw: Any) -> dict[str, Any] | None:
        return self.log("ERROR", event, msg, **kw)


def _otel_ids() -> tuple[str, str]:
    from .tracing import current_ids

    return current_ids()


def set_context(
    *, trace_id: str = "", span_id: str = "", route: str = "", cohort: str = ""
) -> None:
    _trace_id.set(trace_id)
    _span_id.set(span_id)
    _route.set(route)
    _cohort.set(cohort)


def current_context() -> dict[str, str]:
    """Read the active request context.

    Outbound calls propagate from here rather than from the inbound headers, because the
    edge service *mints* the trace ID when the client did not send one. Reading the header
    back would drop the trace at exactly the hop where it starts.
    """
    return {
        "trace_id": _trace_id.get(),
        "span_id": _span_id.get(),
        "route": _route.get(),
        "cohort": _cohort.get(),
    }


class timed:
    """Context manager measuring a block in milliseconds.

    ``duration_ms`` is a first-class field rather than something parsed out of a message,
    so latency questions are answerable with a LogQL aggregation instead of a regex.
    """

    def __init__(self) -> None:
        self.ms = 0.0

    def __enter__(self) -> "timed":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self._t0) * 1000.0
