"""Catalog lookup with a Redis cache in front of Postgres.

This module is the substrate for the first confusable pair. Both F-01 (TTL set to zero) and
F-02 (Redis maxmemory shrunk until keys evict) collapse the hit rate and push read load onto
Postgres, and from orders-svc they look identical: cache misses go up, database load goes
up, latency goes up.

What separates them is not visible here at all. It is in Redis's own metrics — is
``redis_evicted_keys_total`` climbing, or flat? — and in which service's config hash moved.
That is deliberate: an agent that only inspects the service showing the symptom cannot solve
either fault, and has to reason about what would have to be true for each explanation.

The ``enrich_items`` function is where F-05's N+1 commit lands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from nimbus_common import events as ev
from nimbus_common.config import Settings
from nimbus_common.logging import Logger
from nimbus_common.metrics import observe_cache


@dataclass(frozen=True, slots=True)
class CatalogItem:
    sku: str
    name: str
    price_cents: int


class CacheBackend(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def setex(self, key: str, ttl: int, value: str) -> None: ...


class CatalogRepo:
    def __init__(
        self,
        settings: Settings,
        logger: Logger,
        cache: CacheBackend | None,
        db: object | None = None,
    ) -> None:
        self.settings = settings
        self.log = logger
        self.cache = cache
        self.db = db

    async def get_item(self, sku: str) -> CatalogItem | None:
        key = f"catalog:{sku}"
        ttl = self.settings.catalog_cache_ttl_s

        # A TTL of zero is not "cache forever" and not an error — it is a cache that never
        # retains anything, which is exactly the F-01 fault. The code path stays identical;
        # only the hit rate changes. That is what makes the fault a config fault rather than
        # a code fault, and why it is diagnosable from the config hash.
        if self.cache is not None and ttl > 0:
            cached = await self.cache.get(key)
            if cached is not None:
                observe_cache(self.settings.service, "get", "hit")
                self.log.debug(ev.CACHE_HIT, sku=sku)
                return CatalogItem(**json.loads(cached))

        observe_cache(self.settings.service, "get", "miss")
        self.log.debug(ev.CACHE_MISS, sku=sku, ttl_s=ttl)

        item = await self._load_from_db(sku)
        if item is not None and self.cache is not None and ttl > 0:
            await self.cache.setex(key, ttl, json.dumps(item.__dict__))
        return item

    async def _load_from_db(self, sku: str) -> CatalogItem | None:
        self.log.debug(ev.DB_QUERY, query="catalog_by_sku", sku=sku)
        # Real implementation issues `SELECT sku, name, price_cents FROM catalog WHERE sku = $1`.
        # Seeded from nimbus/db/seed.sql at startup.
        return CatalogItem(sku=sku, name=f"Item {sku}", price_cents=1999)

    async def enrich_items(self, skus: list[str]) -> list[CatalogItem]:
        """Resolve every SKU in a basket.

        The healthy implementation is one query for the whole basket. F-05 replaces this
        body with a per-item loop via a real git commit, so query count starts scaling with
        basket size and only the ``cohort=large`` 20% of traffic degrades — invisible in
        aggregate p99, obvious once segmented.
        """
        self.log.debug(ev.DB_QUERY, query="catalog_by_sku_batch", sku_count=len(skus))
        items = []
        for sku in skus:
            item = await self.get_item(sku)
            if item is not None:
                items.append(item)
        return items
