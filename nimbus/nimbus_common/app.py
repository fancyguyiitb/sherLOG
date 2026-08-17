"""FastAPI application factory — the one place request instrumentation is wired.

Every service builds its app through ``create_app`` so that trace context propagation,
cohort tagging, RED metrics and the log schema are identical across all five. A service
that instruments itself differently from its neighbours produces telemetry the agent has to
special-case, and special cases in the evidence layer are how a "generalisable" system
quietly becomes a bespoke one.

The ``cohort`` header is carried from the load generator through every hop and onto every
log line and metric. It is the sole reason F-05 is solvable: an N+1 regression that scales
with basket size is invisible in aggregate percentiles and unmistakable once segmented.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import events as ev
from .config import Settings
from .logging import Logger, current_context, set_context
from .metrics import observe_request

COHORT_HEADER = "x-nimbus-cohort"
TRACE_HEADER = "x-nimbus-trace-id"


def create_app(settings: Settings, logger: Logger) -> FastAPI:
    app = FastAPI(title=settings.service, version=settings.commit_sha)
    app.state.settings = settings
    app.state.log = logger

    @app.middleware("http")
    async def instrument(request: Request, call_next: Callable) -> Response:
        # Accept an inbound trace ID so a checkout is one trace across all five services;
        # mint one at the edge when the caller did not supply it.
        trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        cohort = request.headers.get(COHORT_HEADER, "")
        route = f"{request.method} {request.url.path}"

        set_context(trace_id=trace_id, span_id=span_id, route=route, cohort=cohort)

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception:
            logger.error(ev.HTTP_REQUEST_ERROR, "unhandled exception in request")
            raise
        finally:
            elapsed = time.perf_counter() - started
            # /metrics and /healthz are scraper traffic. Counting them would dilute every
            # SLI with requests no user ever made — the availability ratio would read
            # healthy through an outage purely on the strength of the scrape volume.
            if request.url.path not in ("/metrics", "/healthz"):
                observe_request(settings.service, route, status, cohort, elapsed)
                logger.info(
                    ev.HTTP_REQUEST_END,
                    duration_ms=elapsed * 1000,
                    status=status,
                )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "service": settings.service,
            "commit_sha": settings.commit_sha,
            "config_hash": settings.config_hash,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.on_event("startup")
    async def _startup() -> None:
        logger.info(
            ev.SERVICE_START,
            f"{settings.service} listening on :{settings.port}",
            **{k: v for k, v in settings.as_dict().items() if k != "service"},
        )

    return app


def propagate() -> dict[str, str]:
    """Headers to forward on an outbound call so trace and cohort survive the hop.

    Sourced from the request context, not from the inbound headers: the gateway mints the
    trace ID when the client did not send one, and reading headers back would drop the
    trace at precisely the hop where it begins.
    """
    ctx = current_context()
    headers: dict[str, str] = {}
    if ctx["trace_id"]:
        headers[TRACE_HEADER] = ctx["trace_id"]
    if ctx["cohort"]:
        headers[COHORT_HEADER] = ctx["cohort"]
    return headers
