"""Catalog lookup with a Redis cache in front of Postgres.

This module is the substrate for the first confusable pair. Both F-01 (TTL set to zero) and
F-02 (Redis maxmemory shrunk until keys evict) collapse the hit rate and push read load onto
Postgres, and from orders-svc they look identical: cache misses go up, database load goes
up, latency goes up.

What separates them is not visible here at all. It is in Redis's own metrics — is
``redis_evicted_keys_total`` climbing, or flat? — and in which service's config hash moved.
That is deliberate: an agent that only inspects the service showing the symptom cannot solve
either fault, and has to reason about what would have to be true for each explanation.

Why the cache matters: an item's price is the 30-day median of its observed prices in
``price_history`` — an aggregate over thousands of rows per SKU. That query is genuinely
expensive, which is the whole reason the cache exists, and why losing it hurts.

``enrich_items`` is where F-05's N+1 commit lands.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from nimbus_common import events as ev
from nimbus_common.config import Settings
from nimbus_common.logging import Logger
from nimbus_common.metrics import observe_cache
from nimbus_common.tracing import span

PRICE_SQL = """
SELECT c.sku, c.name, c.description,
       (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY ph.price_cents)
          FROM price_history ph
         WHERE ph.sku = c.sku AND ph.observed_at > now() - interval '30 days') AS price_cents
  FROM catalog c
 WHERE c.sku = ANY(%s)
"""


@dataclass(frozen=True, slots=True)
class CatalogItem:
    sku: str
    name: str
    price_cents: int
    description: str = ""


class CacheBackend(Protocol):
    async def mget(self, keys: list[str]) -> list[Any]: ...
    async def setex(self, key: str, ttl: int, value: str) -> Any: ...


class CatalogRepo:
    def __init__(self, settings: Settings, logger: Logger, cache: CacheBackend | None) -> None:
        self.settings = settings
        self.log = logger
        self.cache = cache

    async def _cache_get(self, skus: list[str]) -> dict[str, CatalogItem]:
        # A TTL of zero is not "cache forever" and not an error — it is a cache that never
        # retains anything, which is exactly the F-01 fault. The code path stays identical;
        # only the hit rate changes.
        if self.cache is None or self.settings.catalog_cache_ttl_s <= 0:
            return {}
        try:
            with span("cache.mget", **{"cache.keys": len(skus)}):
                raw = await self.cache.mget([f"catalog:{s}" for s in skus])
        except Exception as exc:  # cache outage degrades to DB reads, never to an error
            observe_cache(self.settings.service, "get", "error")
            self.log.warn(ev.CACHE_ERROR, "cache read failed", error_type=type(exc).__name__)
            return {}
        found = {}
        for sku, blob in zip(skus, raw):
            if blob is not None:
                found[sku] = CatalogItem(**json.loads(blob))
        return found

    async def _cache_put(self, items: list[CatalogItem]) -> None:
        ttl = self.settings.catalog_cache_ttl_s
        if self.cache is None or ttl <= 0:
            return
        for item in items:
            try:
                await self.cache.setex(f"catalog:{item.sku}", ttl, json.dumps(asdict(item)))
            except Exception:
                observe_cache(self.settings.service, "set", "error")
                return

    async def _load_from_db(self, conn: Any, skus: list[str]) -> list[CatalogItem]:
        with span("db.query", **{"db.statement.name": "catalog_price_batch", "db.rows": len(skus)}):
            cur = await conn.execute(PRICE_SQL, (skus,))
            rows = await cur.fetchall()
        return [CatalogItem(sku=r[0], name=r[1], price_cents=r[3] or 0, description=r[2] or "")
                for r in rows]

    async def enrich_items(self, conn: Any, skus: list[str]) -> list[CatalogItem]:
        """Resolve every SKU in a basket: one cache round-trip, then one batched query for
        whatever missed. F-05 replaces this body with a per-item loop via a real commit."""
        unique = list(dict.fromkeys(skus))
        hits = await self._cache_get(unique)
        misses = [s for s in unique if s not in hits]
        for _ in hits:
            observe_cache(self.settings.service, "get", "hit")
        for _ in misses:
            observe_cache(self.settings.service, "get", "miss")

        loaded: list[CatalogItem] = []
        if misses:
            self.log.info(ev.DB_QUERY, query="catalog_price_batch", sku_count=len(misses),
                          cache_hits=len(hits))
            loaded = await self._load_from_db(conn, misses)
            await self._cache_put(loaded)

        by_sku = {**hits, **{i.sku: i for i in loaded}}
        return [by_sku[s] for s in skus if s in by_sku]
