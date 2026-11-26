"""OpenTelemetry tracing, exported to Tempo over OTLP gRPC.

One trace per checkout across every hop: FastAPI server spans on the way in, httpx client
spans on the way out (W3C ``traceparent`` is injected automatically), plus a handful of
hand-made spans around the operations the fault catalog turns on — ``db.acquire``,
``db.query``, ``cache.mget``. Those names are what ``trace_search`` breaks durations down by.

The trace ID stamped on log lines is the OTel trace ID, so a ``trace_id`` lifted from a log
line resolves in Tempo. That join is the single most valuable path in the evidence layer.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def telemetry_enabled() -> bool:
    """``TELEMETRY=off`` is the F-07 blind-spot switch: no traces, no logs, no /metrics."""
    return os.environ.get("TELEMETRY", "on").lower() != "off"


def setup(service: str, endpoint: str) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "service.namespace": "nimbus"})
    )
    if endpoint and telemetry_enabled():
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
    trace.set_tracer_provider(provider)

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def tracer() -> trace.Tracer:
    return trace.get_tracer("nimbus")


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    with tracer().start_as_current_span(name) as s:
        for k, v in attrs.items():
            s.set_attribute(k, v)
        yield s


def current_ids() -> tuple[str, str]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return "", ""
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
