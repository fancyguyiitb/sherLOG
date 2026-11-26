# Runbook: orders-svc database pool saturation

## Symptoms
gateway /checkout returns 504s; orders-svc logs `db.acquire.timeout`; `db_pool_waiters`
rises and `db_pool_in_use` sits at `db_pool_size`. Traces show time inside `db.acquire`.

## Important: saturation is often a symptom
orders-svc holds its DB connection while it calls payments-svc. Anything that makes the
payment call slow therefore holds connections longer and saturates the pool, even though
the pool is correctly sized. Before blaming the pool:
1. Did `db_pool_size` change? Did orders-svc's config_hash change or the service restart?
2. Is `upstream_duration_seconds{upstream="payments-svc"}` (orders-svc's view) or
   `upstream_duration_seconds{upstream="psp"}` (payments-svc's view) elevated?
A shrunken pool with healthy upstreams is a pool problem. A normal-sized pool with slow
upstreams is an upstream problem that looks like a pool problem.

## Mitigation
Restore pool sizing if it changed. If upstreams are slow, fix the upstream; consider
releasing the connection before calling payments.
