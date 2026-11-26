"""worker — the fulfillment job loop, and the substrate for F-06.

Polls the ``jobs`` table with ``FOR UPDATE SKIP LOCKED`` in id order, checks every SKU in
the order has a list price, decrements inventory, and marks the order COMPLETE. It never
touches the request path, which is precisely why it matters: a worker that stops working
breaches nothing that availability or latency SLOs measure. Orders keep being accepted,
every request returns 200 in good time, and the system is quietly broken.

The freshness SLO — orders reaching COMPLETE within 60 seconds, against orders created —
is the only thing that notices. F-06 exists to prove the system can diagnose an incident
that produces no errors and no latency at all.

The dead-letter branch below is what F-06 removes, via a real commit. Without it, one job
that always raises is retried forever at the head of the queue.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import psycopg
from prometheus_client import start_http_server

from nimbus_common import events as ev
from nimbus_common import tracing
from nimbus_common.config import Settings, load
from nimbus_common.logging import Logger
from nimbus_common.metrics import (
    job_attempts_total,
    job_oldest_age_seconds,
    order_completion_seconds,
    queue_depth,
)

QUEUE = "fulfillment"
RETRY_BACKOFF_S = 1.0

CLAIM_SQL = """
SELECT id, payload, attempts FROM jobs
 WHERE queue = %s AND status = 'PENDING'
 ORDER BY id
 LIMIT 1
 FOR UPDATE SKIP LOCKED
"""


class HandlerError(Exception):
    """A job the handler could not process. Retryable until attempts are exhausted."""


class Worker:
    def __init__(self, settings: Settings, logger: Logger) -> None:
        self.settings = settings
        self.log = logger

    async def run_once(self, conn: psycopg.AsyncConnection) -> bool:
        """Claim one job, run it, settle it. Returns False when the queue is empty."""
        await self._publish_gauges(conn)
        async with conn.transaction():
            cur = await conn.execute(CLAIM_SQL, (QUEUE,))
            row = await cur.fetchone()
            if row is None:
                return False
            job_id, payload, attempts = row[0], row[1], row[2] + 1
            if isinstance(payload, str):
                payload = json.loads(payload)

            with tracing.span("job.process", **{"job.id": job_id, "job.attempt": attempts}):
                self.log.info(ev.JOB_CLAIMED, job_id=job_id, attempt=attempts, queue=QUEUE)
                try:
                    await self._fulfill(conn, payload)
                except Exception as exc:
                    await self._settle_failure(conn, job_id, payload, attempts, exc)
                    return True

                await conn.execute(
                    "UPDATE jobs SET status='DONE', attempts=%s WHERE id=%s", (attempts, job_id)
                )
                cur = await conn.execute(
                    "UPDATE orders SET status='COMPLETE', completed_at=now() WHERE id=%s "
                    "RETURNING extract(epoch FROM completed_at - created_at)",
                    (payload["order_id"],),
                )
                age = await cur.fetchone()
                age_s = float(age[0]) if age else 0.0
                job_attempts_total.labels(QUEUE, "complete").inc()
                order_completion_seconds.observe(age_s)
                self.log.info(ev.JOB_COMPLETE, job_id=job_id, duration_ms=age_s * 1000,
                              attempts=attempts)
        return True

    async def _settle_failure(
        self, conn: psycopg.AsyncConnection, job_id: int, payload: dict[str, Any],
        attempts: int, exc: Exception,
    ) -> None:
        self.log.error(
            ev.JOB_HANDLER_ERROR,
            "fulfillment handler raised",
            job_id=job_id,
            attempt=attempts,
            error_type=type(exc).__name__,
            error=str(exc),
        )

        # ---------------------------------------------------------------------------
        # THE F-06 FAULT SITE.
        #
        # F-06 ships a real commit ("refactor: streamline job retry handling") that deletes
        # this branch. Without it a job that always raises stays PENDING at the head of the
        # queue forever, every subsequent order stops completing, and queue_depth and
        # job_oldest_age_seconds climb without a single error on the request path.
        # ---------------------------------------------------------------------------
        if attempts >= self.settings.job_max_attempts:
            job_attempts_total.labels(QUEUE, "dead_letter").inc()
            await conn.execute(
                "UPDATE jobs SET status='DEAD', attempts=%s, last_error=%s WHERE id=%s",
                (attempts, str(exc), job_id),
            )
            self.log.warn(
                ev.JOB_DEAD_LETTERED,
                "job exceeded max attempts and was dead-lettered",
                job_id=job_id,
                attempts=attempts,
            )
            return

        job_attempts_total.labels(QUEUE, "retry").inc()
        await conn.execute(
            "UPDATE jobs SET attempts=%s, last_error=%s WHERE id=%s", (attempts, str(exc), job_id)
        )
        self.log.warn(ev.JOB_RETRY, job_id=job_id, attempt=attempts)
        await asyncio.sleep(RETRY_BACKOFF_S)

    async def _fulfill(self, conn: psycopg.AsyncConnection, payload: dict[str, Any]) -> None:
        """Check list prices, decrement inventory. Raises on a SKU with no list price."""
        skus = payload.get("skus", [])
        cur = await conn.execute(
            "SELECT sku, price_cents FROM catalog WHERE sku = ANY(%s)", (skus,)
        )
        for sku, price in await cur.fetchall():
            if price is None:
                # A real defect reachable from real data, rather than a flag.
                raise HandlerError(f"null list price for sku {sku}")
        await conn.execute(
            "UPDATE inventory SET quantity = quantity - 1 WHERE sku = ANY(%s)", (skus,)
        )

    async def _publish_gauges(self, conn: psycopg.AsyncConnection) -> None:
        cur = await conn.execute(
            "SELECT count(*), coalesce(extract(epoch FROM now() - min(enqueued_at)), 0) "
            "FROM jobs WHERE queue=%s AND status='PENDING'",
            (QUEUE,),
        )
        depth, oldest = await cur.fetchone()  # type: ignore[misc]
        queue_depth.labels(QUEUE).set(depth)
        job_oldest_age_seconds.labels(QUEUE).set(float(oldest))

    async def run_forever(self) -> None:
        self.log.info(ev.SERVICE_START, "worker polling for jobs", queue=QUEUE,
                      config_hash=self.settings.config_hash)
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.settings.database_url, autocommit=True
                ) as conn:
                    while True:
                        if not await self.run_once(conn):
                            await asyncio.sleep(self.settings.job_poll_interval_s)
            except psycopg.OperationalError as exc:
                self.log.error(ev.DB_ERROR, "worker lost its database connection",
                               error_type=type(exc).__name__)
                await asyncio.sleep(2.0)


def main() -> None:
    settings = load("worker", port=8004)
    tracing.setup("worker", settings.otlp_endpoint)
    if tracing.telemetry_enabled():
        start_http_server(int(os.environ.get("PORT", "8004")))
    asyncio.run(Worker(settings, Logger(settings)).run_forever())


if __name__ == "__main__":
    main()
