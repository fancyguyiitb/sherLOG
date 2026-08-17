"""worker — the fulfillment job loop, and the substrate for F-06.

Polls the ``jobs`` table with ``FOR UPDATE SKIP LOCKED``, decrements inventory, and marks
orders COMPLETE. It never touches the request path, which is precisely why it matters: a
worker that stops working breaches nothing that availability or latency SLOs measure. Orders
keep being accepted, every request returns 200 in good time, and the system is quietly
broken.

The freshness SLO — the fraction of orders reaching COMPLETE within 60 seconds — is the only
thing that notices. F-06 exists to prove the system can diagnose an incident that produces
no errors and no latency at all.

The dead-letter branch below is what F-06 removes, via a real commit. Without it, one job
that always raises retries forever and blocks the head of the queue.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from nimbus_common import events as ev
from nimbus_common.config import Settings, load
from nimbus_common.logging import Logger
from nimbus_common.metrics import (
    job_attempts_total,
    job_oldest_age_seconds,
    order_completion_seconds,
    queue_depth,
)

QUEUE = "fulfillment"


class HandlerError(Exception):
    """A job the handler could not process. Retryable until attempts are exhausted."""


@dataclass
class Job:
    id: str
    payload: dict[str, Any]
    attempts: int = 0
    enqueued_at: float = field(default_factory=time.time)

    @property
    def age_s(self) -> float:
        return time.time() - self.enqueued_at


class Worker:
    def __init__(
        self,
        settings: Settings,
        logger: Logger,
        handler: Callable[[Job], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.log = logger
        self.handler = handler or self._fulfill
        self.queue: list[Job] = []
        self.dead_letter: list[Job] = []

    async def run_once(self) -> Job | None:
        """Claim one job, run it, and settle it. Returns the job it handled, if any."""
        self._publish_gauges()
        if not self.queue:
            return None

        job = self.queue.pop(0)
        job.attempts += 1
        self.log.info(ev.JOB_CLAIMED, job_id=job.id, attempt=job.attempts, queue=QUEUE)

        try:
            await self._maybe_await(self.handler(job))
        except Exception as exc:
            return self._settle_failure(job, exc)

        job_attempts_total.labels(QUEUE, "complete").inc()
        order_completion_seconds.observe(job.age_s)
        self.log.info(
            ev.JOB_COMPLETE,
            job_id=job.id,
            duration_ms=job.age_s * 1000,
            attempts=job.attempts,
        )
        return job

    def _settle_failure(self, job: Job, exc: Exception) -> Job:
        self.log.error(
            ev.JOB_HANDLER_ERROR,
            "fulfillment handler raised",
            job_id=job.id,
            attempt=job.attempts,
            error_type=type(exc).__name__,
            payload_sku=job.payload.get("sku"),
        )

        # ---------------------------------------------------------------------------
        # THE F-06 FAULT SITE.
        #
        # F-06 ships a real commit ("refactor: streamline job retry handling") that deletes
        # this branch. Without it a job that always raises is re-enqueued at the head of the
        # queue forever, every subsequent order stops completing, and queue_depth and
        # job_oldest_age_seconds climb without a single error appearing on the request path.
        #
        # The bad row is the trigger; this missing branch is the root cause. Ground truth
        # names the code, and an answer that stops at "there is a bad row" scores as partial.
        # ---------------------------------------------------------------------------
        if job.attempts >= self.settings.job_max_attempts:
            job_attempts_total.labels(QUEUE, "dead_letter").inc()
            self.dead_letter.append(job)
            self.log.warn(
                ev.JOB_DEAD_LETTERED,
                "job exceeded max attempts and was dead-lettered",
                job_id=job.id,
                attempts=job.attempts,
            )
            return job

        job_attempts_total.labels(QUEUE, "retry").inc()
        self.queue.insert(0, job)  # head-of-line: preserves ordering, and is what hurts
        self.log.warn(ev.JOB_RETRY, job_id=job.id, attempt=job.attempts)
        return job

    async def _fulfill(self, job: Job) -> None:
        """Decrement inventory and mark the order COMPLETE."""
        price = job.payload.get("price_cents")
        if price is None:
            # The crafted F-06 job carries a null price. A real defect, reachable from real
            # data, rather than a flag we set to pretend one exists.
            raise HandlerError(f"null price for sku {job.payload.get('sku')}")

    @staticmethod
    async def _maybe_await(result: Any) -> Any:
        return await result if asyncio.iscoroutine(result) else result

    def _publish_gauges(self) -> None:
        queue_depth.labels(QUEUE).set(len(self.queue))
        oldest = max((j.age_s for j in self.queue), default=0.0)
        job_oldest_age_seconds.labels(QUEUE).set(oldest)

    async def run_forever(self) -> None:
        self.log.info(ev.SERVICE_START, "worker polling for jobs", queue=QUEUE)
        while True:
            handled = await self.run_once()
            if handled is None:
                await asyncio.sleep(self.settings.job_poll_interval_s)


def main() -> None:
    settings = load("worker")
    worker = Worker(settings, Logger(settings))
    asyncio.run(worker.run_forever())


if __name__ == "__main__":
    main()
