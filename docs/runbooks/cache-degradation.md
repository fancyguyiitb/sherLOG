# Runbook: catalog cache degradation

## Symptoms
Checkout latency rises, mostly for large baskets, with few or no errors. orders-svc
`db.query` volume (event catalog_price_batch) rises and the cache hit rate
(`discriminator:cache_miss_rate`, `cache_ops_total{result="miss"}`) climbs. Postgres commit
rate rises. Price lookups are an aggregate over price_history and cost ~20ms per uncached SKU.

## Distinguishing causes
Two very different faults produce this same picture:
- **Cache retention disabled by configuration.** orders-svc's CATALOG_CACHE_TTL_S controls
  whether lookups are cached at all; at 0 nothing is ever stored. Check whether orders-svc's
  config_hash changed (log_stats identity lines) and whether it restarted (runtime_state).
  Redis itself is healthy: evictions flat, memory well under the limit.
- **Redis evicting keys.** If Redis maxmemory is too small for the working set (~12 MB of
  catalog entries), allkeys-lru evicts continuously. `redis_evicted_keys_total` climbs and
  `redis_memory_used_bytes` sits at `redis_memory_max_bytes`. orders-svc config is unchanged.
Check the discriminator before acting; the fixes differ.

## Mitigation
Restore the TTL, or restore Redis maxmemory. Alert on hit rate, not only on latency.
