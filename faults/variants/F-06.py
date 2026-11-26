"""F-06 — "refactor: streamline job retry handling" removes the dead-letter branch.

Harmless until a job that always fails arrives; then it retries at the head of the queue
forever and no order completes again.
"""

EDITS = [
    {
        "op": "replace_method",
        "file": "nimbus/worker/main.py",
        "class": "Worker",
        "method": "_settle_failure",
        "source": '''
async def _settle_failure(self, conn: psycopg.AsyncConnection, job_id: int, payload: dict[str, Any], attempts: int, exc: Exception) -> None:
    self.log.error(ev.JOB_HANDLER_ERROR, 'fulfillment handler raised', job_id=job_id, attempt=attempts, error_type=type(exc).__name__, error=str(exc))
    job_attempts_total.labels(QUEUE, 'retry').inc()
    await conn.execute('UPDATE jobs SET attempts=%s, last_error=%s WHERE id=%s', (attempts, str(exc), job_id))
    self.log.warn(ev.JOB_RETRY, job_id=job_id, attempt=attempts)
    await asyncio.sleep(RETRY_BACKOFF_S)
''',
    }
]
