"""Instrumented HTTP client for service-to-service calls.

Every outbound call is timed and recorded against ``upstream_duration_seconds``, labelled
by the upstream it targeted. That histogram is the single discriminator between F-03 and
F-04: when the orders pool is saturated, the question "is the pool the cause, or is it
holding connections open because it is blocked on payments?" is answered by whether the psp
upstream is slow. Without per-upstream timing the two faults are genuinely
indistinguishable and the agent is guessing.

The client also emits ``upstream.slow`` above a threshold, so the same fact is visible from
the log plane without a metrics query — cross-signal corroboration is what the assessment
stage is asked to look for, and it can only find it if both signals exist.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import events as ev
from .config import Settings
from .logging import Logger
from .metrics import observe_upstream

#: An upstream call above this is logged as slow regardless of whether it eventually
#: succeeded. Set well under the client timeout so degradation is visible before failure.
SLOW_THRESHOLD_S = 0.5


class UpstreamClient:
    """Thin wrapper over httpx that owns timeout, retry and instrumentation policy."""

    def __init__(self, settings: Settings, logger: Logger, upstream: str, base_url: str) -> None:
        self.settings = settings
        self.log = logger
        self.upstream = upstream
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(settings.upstream_timeout_s),
        )

    async def post(self, path: str, json: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._call("POST", path, json=json, headers=headers)

    async def get(self, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._call("GET", path, headers=headers)

    async def _call(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        attempts = self.settings.upstream_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                response = await self.client.request(method, path, json=json, headers=headers)
                elapsed = time.perf_counter() - started
                observe_upstream(
                    self.settings.service, self.upstream, str(response.status_code), elapsed
                )

                if elapsed >= SLOW_THRESHOLD_S:
                    self.log.warn(
                        ev.UPSTREAM_SLOW,
                        "upstream call exceeded the slow threshold",
                        duration_ms=elapsed * 1000,
                        upstream=self.upstream,
                        path=path,
                        attempt=attempt,
                    )

                response.raise_for_status()
                return response.json()

            except httpx.TimeoutException as exc:
                elapsed = time.perf_counter() - started
                observe_upstream(self.settings.service, self.upstream, "timeout", elapsed)
                self.log.error(
                    ev.UPSTREAM_TIMEOUT,
                    "upstream call timed out",
                    duration_ms=elapsed * 1000,
                    upstream=self.upstream,
                    path=path,
                    attempt=attempt,
                    timeout_s=self.settings.upstream_timeout_s,
                )
                last_error = exc

            except httpx.HTTPStatusError as exc:
                elapsed = time.perf_counter() - started
                self.log.error(
                    ev.UPSTREAM_ERROR,
                    "upstream returned an error status",
                    duration_ms=elapsed * 1000,
                    upstream=self.upstream,
                    path=path,
                    status=exc.response.status_code,
                    attempt=attempt,
                )
                # 4xx is a decision, not a fault. Retrying it just doubles the load on a
                # service that already told us the answer.
                if exc.response.status_code < 500:
                    raise
                last_error = exc

        assert last_error is not None
        raise last_error

    async def aclose(self) -> None:
        await self.client.aclose()
