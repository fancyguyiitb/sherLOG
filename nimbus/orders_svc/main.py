"""orders-svc — the busiest service and the one most faults route through.

Owns the Postgres pool and the Redis client, calls payments-svc, and enqueues a fulfillment
job. Three of the six v1 faults have their cause here (F-01, F-03, F-05) and two more have
their most visible *symptom* here while the cause lives elsewhere (F-02 in Redis, F-04 in
psp-stub). That overlap is intentional: "the service that hurts" and "the service at fault"
being different is the thing that makes root-cause analysis harder than alert routing.
"""

from __future__ import annotations

import json
import uuid

import redis.asyncio as aioredis
from fastapi import HTTPException
from pydantic import BaseModel

from nimbus_common import events as ev
from nimbus_common.app import create_app, propagate
from nimbus_common.config import load
from nimbus_common.http import UpstreamClient
from nimbus_common.logging import Logger, timed
from nimbus_common.metrics import orders_created_total

from .catalog import CatalogRepo
from .pool import InstrumentedPool, PoolTimeout

settings = load("orders-svc", port=8001)
log = Logger(settings)
app = create_app(settings, log)

pool = InstrumentedPool(settings, log)
cache = aioredis.from_url(settings.redis_url, socket_timeout=1.0) if settings.redis_url else None
catalog = CatalogRepo(settings, log, cache=cache)
payments = UpstreamClient(settings, log, upstream="payments-svc", base_url="http://payments-svc:8002")


class LineItem(BaseModel):
    sku: str
    quantity: int = 1


class CreateOrder(BaseModel):
    customer_id: str
    items: list[LineItem]
    payment_method: str = "card"


@app.post("/orders")
async def create_order(req: CreateOrder) -> dict[str, object]:
    order_id = str(uuid.uuid4())

    try:
        async with pool.acquire() as conn:
            items = await catalog.enrich_items(conn, [i.sku for i in req.items])
            total_cents = sum(
                item.price_cents * line.quantity
                for item, line in zip(items, req.items)
            )
            log.info(ev.ORDER_CREATED, order_id=order_id, total_cents=total_cents, **pool.stats)

            # The payment call happens while a connection is still held. This is a real
            # anti-pattern and it is here on purpose: it is the mechanism by which F-04
            # (slow external provider) manifests as pool saturation, which is what makes
            # F-03 and F-04 confusable in the first place.
            with timed() as t:
                payment = await payments.post(
                    "/payments",
                    json={
                        "order_id": order_id,
                        "amount_cents": total_cents,
                        "payment_method": req.payment_method,
                        "idempotency_key": order_id,
                    },
                    headers=propagate(),
                )

            if payment.get("status") == "declined":
                log.warn(ev.PAYMENT_DECLINED, order_id=order_id, duration_ms=t.ms)
                raise HTTPException(status_code=402, detail="payment declined")

            log.info(ev.PAYMENT_CAPTURED, order_id=order_id, duration_ms=t.ms)
            await _persist(conn, order_id, req, items, total_cents)

    except PoolTimeout as exc:
        # A 500 rather than a 503: the pool being starved is our defect, not backpressure
        # we are deliberately applying, and labelling it as the latter would let the
        # availability SLO stay green through the exact failure it exists to catch.
        raise HTTPException(status_code=500, detail="database unavailable") from exc

    return {"order_id": order_id, "status": "PENDING", "total_cents": total_cents}


@app.get("/orders/{order_id}")
async def get_order(order_id: str) -> dict[str, object]:
    try:
        uuid.UUID(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no such order") from exc
    try:
        async with pool.acquire() as conn:
            log.debug(ev.DB_QUERY, query="order_by_id", order_id=order_id)
            # Under F-06 this returns a stale PENDING indefinitely: the worker never
            # completes the job, so the read is correct, fast, and wrong. Zero errors
            # anywhere, and the freshness SLO is the only thing that notices.
            cur = await conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
            row = await cur.fetchone()
    except PoolTimeout as exc:
        raise HTTPException(status_code=500, detail="database unavailable") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="no such order")
    return {"order_id": order_id, "status": row[0]}


async def _persist(conn: object, order_id: str, req: CreateOrder, items: list, total: int) -> None:
    """Order row, line items and the fulfillment job, in one transaction."""
    log.debug(ev.DB_QUERY, query="insert_order_and_enqueue", order_id=order_id)
    async with conn.transaction():  # type: ignore[attr-defined]
        await conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO orders (id, customer_id, status, total_cents) VALUES (%s,%s,'PENDING',%s)",
            (order_id, req.customer_id, total),
        )
        await conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO jobs (queue, payload) VALUES ('fulfillment', %s)",
            (json.dumps({"order_id": order_id, "skus": [i.sku for i in req.items]}),),
        )
    orders_created_total.inc()


@app.on_event("startup")
async def _open_pool() -> None:
    await pool.open()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await pool.close()
    await payments.aclose()
    log.info(ev.SERVICE_STOP, "orders-svc shutting down")
