# Runbook: fulfillment backlog / orders stuck PENDING

## Symptoms
Freshness SLO burns (orders not COMPLETE within 60s). Request path is healthy: no errors,
no latency. `queue_depth{queue="fulfillment"}` and `job_oldest_age_seconds` climb;
`job_attempts_total{status="complete"}` falls toward zero.

## Diagnosis
The worker processes jobs strictly in id order. Look at worker logs: `job.handler.error`
and `job.retry` repeating for the same job_id means one job is failing repeatedly at the
head of the queue. By design a job that fails JOB_MAX_ATTEMPTS times is dead-lettered
(`job.dead_lettered`, `job_attempts_total{status="dead_letter"}`) so the queue keeps moving.
If a job is retrying past that limit, the dead-letter path is not working — check recent
deploys to worker (git_diff_since). The bad job itself (e.g. a catalog row with no list
price) is the trigger, but the missing dead-letter handling is why one row stops the queue.

## Mitigation
Dead-letter the stuck job manually; restore dead-letter handling.
