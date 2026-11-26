"""gateway — the public edge. Stateless, no database.

It owns the timeout and retry policy for the whole checkout path, which is what makes it
the place every fault eventually becomes visible: a saturated pool two hops away surfaces
here as a 504, and psp latency three hops away surfaces here as a timeout. The SLOs are
defined at this boundary because it is the only place that sees what a user sees.

The symptom being two or three hops from the cause is the entire point of the topology. If
the gateway could name the cause, there would be nothing to diagnose.
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel

from nimbus_common import events as ev
from nimbus_common.app import create_app, propagate
from nimbus_common.config import load
from nimbus_common.http import UpstreamClient
from nimbus_common.logging import Logger, timed

settings = load("gateway", port=8000)
log = Logger(settings)
app = create_app(settings, log)

orders = UpstreamClient(settings, log, upstream="orders-svc", base_url="http://orders-svc:8001")


class LineItem(BaseModel):
    sku: str
    quantity: int = 1


class CheckoutRequest(BaseModel):
    customer_id: str
    items: list[LineItem]
    payment_method: str = "card"


@app.post("/checkout")
async def checkout(req: CheckoutRequest, request: Request) -> dict[str, object]:
    log.info(ev.CHECKOUT_START, item_count=len(req.items))

    with timed() as t:
        try:
            result = await orders.post(
                "/orders",
                json=req.model_dump(),
                headers=propagate(),
            )
        except httpx.HTTPStatusError as exc:
            # A 4xx from orders-svc is an answer (a declined card is a 402), not an outage.
            # Passing it through keeps the 2% decline floor out of the availability SLI.
            if exc.response.status_code < 500:
                raise HTTPException(status_code=exc.response.status_code,
                                    detail=exc.response.json().get("detail")) from exc
            log.error(
                ev.CHECKOUT_FAILED,
                "checkout failed at the orders-svc call",
                duration_ms=t.ms,
                error_type=type(exc).__name__,
            )
            raise HTTPException(status_code=504, detail="checkout unavailable") from exc
        except Exception as exc:
            log.error(
                ev.CHECKOUT_FAILED,
                "checkout failed at the orders-svc call",
                duration_ms=t.ms,
                error_type=type(exc).__name__,
            )
            # 504 rather than 500: the gateway itself is healthy, and conflating "we are
            # broken" with "our dependency is" would send the on-call to the wrong service.
            raise HTTPException(status_code=504, detail="checkout unavailable") from exc

    log.info(
        ev.CHECKOUT_COMPLETE,
        duration_ms=t.ms,
        order_id=result.get("order_id"),
    )
    return result


@app.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request) -> dict[str, object]:
    try:
        return await orders.get(f"/orders/{order_id}", headers=propagate())
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            raise HTTPException(status_code=exc.response.status_code, detail="not found") from exc
        raise HTTPException(status_code=504, detail="orders unavailable") from exc
    except Exception as exc:
        raise HTTPException(status_code=504, detail="orders unavailable") from exc


@app.on_event("shutdown")
async def _shutdown() -> None:
    await orders.aclose()
    log.info(ev.SERVICE_STOP, "gateway shutting down")
