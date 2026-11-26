"""F-05 — N+1 regression shipped as "perf: simplify item enrichment logic".

Replaces the batched, cache-fronted lookup with a per-item loop straight to Postgres. Only
large baskets degrade, so it is invisible in aggregate and obvious once split by cohort.
"""

EDITS = [
    {
        "op": "replace_method",
        "file": "nimbus/orders_svc/catalog.py",
        "class": "CatalogRepo",
        "method": "enrich_items",
        "source": '''
async def enrich_items(self, conn: Any, skus: list[str]) -> list[CatalogItem]:
    items: list[CatalogItem] = []
    for sku in skus:
        self.log.info(ev.DB_QUERY, query='catalog_price_by_sku', sku=sku)
        items.extend(await self._load_from_db(conn, [sku]))
    return items
''',
    }
]
