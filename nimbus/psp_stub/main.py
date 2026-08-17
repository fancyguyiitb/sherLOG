"""psp-stub — a fake external payment provider.

Stands in for Stripe. It exists so that "the external dependency got slow" is a fault we
can inject at the network layer (Toxiproxy sits between payments-svc and this service)
rather than a flag inside our own code. The distinction matters: an `if FAULT: sleep()`
inside payments-svc would make F-04 unsolvable-by-honest-means, because the evidence would
live in code the agent is meant to be diagnosing from the outside.

Normal response is 40-80ms, which is a plausible card-capture latency and slow enough that
a 1500ms toxic is a clear signal rather than a rounding error.
"""

from __future__ import annotations

import asyncio
import random

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="psp-stub")

BASELINE_MIN_S = 0.040
BASELINE_MAX_S = 0.080


class CaptureRequest(BaseModel):
    amount_cents: int
    currency: str = "USD"
    payment_method: str
    idempotency_key: str


@app.post("/capture")
async def capture(req: CaptureRequest) -> dict[str, str | int]:
    await asyncio.sleep(random.uniform(BASELINE_MIN_S, BASELINE_MAX_S))

    # The 2% invalid-payment-method share of traffic declines here. That gives the system a
    # steady non-zero error floor: without one, any error at all is trivially the incident,
    # and the agent never has to reason about a delta.
    if req.payment_method == "invalid-card":
        return {"status": "declined", "reason": "invalid_payment_method", "amount_cents": req.amount_cents}

    return {
        "status": "captured",
        "provider_ref": f"psp_{req.idempotency_key[:12]}",
        "amount_cents": req.amount_cents,
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "psp-stub"}
