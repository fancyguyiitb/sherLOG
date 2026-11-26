"""An instrumented connection pool.

asyncpg has its own pool, but it does not expose waiter counts, and ``db_pool_waiters`` is
the signal F-03 turns on. So the pool is wrapped in a semaphore we can see through.

The instrumentation is the point. F-03 (the pool really is too small) and F-04 (the pool is
saturated because handlers are blocked on a slow payment provider) produce *identical*
waiter counts. Distinguishing them needs ``db_pool_size`` — did the configured maximum
actually change? — alongside per-upstream timing. Exposing size, in-use and waiters
separately is what makes that question answerable at all.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from psycopg_pool import AsyncConnectionPool

from nimbus_common import events as ev
from nimbus_common.config import Settings
from nimbus_common.tracing import span
from nimbus_common.logging import Logger
from nimbus_common.metrics import (
    db_pool_acquire_seconds,
    db_pool_in_use,
    db_pool_size,
    db_pool_waiters,
)


class PoolTimeout(Exception):
    """Acquisition exceeded ``DB_ACQUIRE_TIMEOUT_S``. Surfaces to the caller as a 500."""


class InstrumentedPool:
    """Semaphore-guarded pool with the gauges the fault catalog is written against."""

    def __init__(self, settings: Settings, logger: Logger, connect: object = None) -> None:
        self.settings = settings
        self.log = logger
        self._sem = asyncio.Semaphore(settings.db_pool_max)
        # The real psycopg pool sits underneath the semaphore, sized identically, so the
        # semaphore is a transparent window onto it rather than a second limit.
        self._pg: AsyncConnectionPool | None = None
        if settings.database_url:
            self._pg = AsyncConnectionPool(
                settings.database_url,
                min_size=min(settings.db_pool_min, settings.db_pool_max),
                max_size=settings.db_pool_max,
                open=False,
                kwargs={"autocommit": True},
            )
        self._connect = connect
        self._in_use = 0
        self._waiters = 0
        db_pool_size.labels(settings.service).set(settings.db_pool_max)
        db_pool_in_use.labels(settings.service).set(0)
        db_pool_waiters.labels(settings.service).set(0)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[object]:
        service = self.settings.service
        self._waiters += 1
        db_pool_waiters.labels(service).set(self._waiters)
        started = time.perf_counter()

        try:
            with span("db.acquire", **{"db.pool.size": self.settings.db_pool_max}):
                await asyncio.wait_for(
                    self._sem.acquire(), timeout=self.settings.db_acquire_timeout_s
                )
        except asyncio.TimeoutError as exc:
            waited = time.perf_counter() - started
            db_pool_acquire_seconds.labels(service).observe(waited)
            self.log.error(
                ev.DB_ACQUIRE_TIMEOUT,
                f"connection acquisition timed out after "
                f"{self.settings.db_acquire_timeout_s * 1000:.0f}ms",
                duration_ms=waited * 1000,
                pool_size=self.settings.db_pool_max,
                waiters=self._waiters,
                in_use=self._in_use,
            )
            raise PoolTimeout(
                f"could not acquire a connection within {self.settings.db_acquire_timeout_s}s"
            ) from exc
        finally:
            self._waiters -= 1
            db_pool_waiters.labels(service).set(self._waiters)

        waited = time.perf_counter() - started
        db_pool_acquire_seconds.labels(service).observe(waited)
        self._in_use += 1
        db_pool_in_use.labels(service).set(self._in_use)

        try:
            if self._pg is None:
                yield self._connect
            else:
                async with self._pg.connection() as conn:
                    yield conn
        finally:
            self._in_use -= 1
            db_pool_in_use.labels(service).set(self._in_use)
            self._sem.release()

    async def open(self) -> None:
        if self._pg is not None:
            await self._pg.open(wait=True, timeout=30)

    async def close(self) -> None:
        if self._pg is not None:
            await self._pg.close()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "size": self.settings.db_pool_max,
            "in_use": self._in_use,
            "waiters": self._waiters,
        }
