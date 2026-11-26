"""payments-svc — calls the external provider through Toxiproxy.

The base URL points at Toxiproxy rather than at psp-stub directly. That indirection is what
lets F-04 be injected as a real L4 latency toxic on a real network hop, instead of a sleep
inside our own code. The difference is not cosmetic: with a toxic, the slow span shows up in
traces as time spent inside the psp call, exactly as a genuinely slow vendor would look. With
a sleep, it would show up as time spent inside payments-svc, and the agent would be
diagnosing a fiction.

payments-svc is also the service silenced in F-07: its log shipper is stopped, metrics keep
flowing, and the correct answer becomes "payments-svc is degraded and I cannot say why".
"""

from __future__ import annotations

import os

from fastapi import HTTPException
from pydantic import BaseModel

from nimbus_common import events as ev
from nimbus_common.app import create_app, propagate
from nimbus_common.config import load
from nimbus_common.http import UpstreamClient
from nimbus_common.logging import Logger, timed

settings = load("payments-svc", port=8002)
log = Logger(settings)
app = create_app(settings, log)

# Through the chaos proxy, never directly at psp-stub.
PSP_BASE_URL = os.environ.get("PSP_BASE_URL", "http://toxiproxy:8666")
psp = UpstreamClient(settings, log, upstream="psp", base_url=PSP_BASE_URL)

#: Idempotency keys already captured. Postgres-backed in the real thing; in-process here
#: because payments-svc runs as a single replica and the retry semantics are what matter.
_captured: dict[str, dict[str, object]] = {}


class PaymentRequest(BaseModel):
    order_id: str
    amount_cents: int
    payment_method: str = "card"
    idempotency_key: str


@app.post("/payments")
async def create_payment(req: PaymentRequest) -> dict[str, object]:
    if req.idempotency_key in _captured:
        return _captured[req.idempotency_key]

    with timed() as t:
        try:
            result = await psp.post(
                "/capture",
                json={
                    "amount_cents": req.amount_cents,
                    "payment_method": req.payment_method,
                    "idempotency_key": req.idempotency_key,
                },
                headers=propagate(),
            )
        except Exception as exc:
            log.error(
                ev.UPSTREAM_ERROR,
                "payment provider call failed",
                duration_ms=t.ms,
                order_id=req.order_id,
                error_type=type(exc).__name__,
            )
            raise HTTPException(status_code=502, detail="payment provider unavailable") from exc

    payment = {
        "payment_id": f"pay_{req.idempotency_key[:12]}",
        "order_id": req.order_id,
        "status": result.get("status"),
        "amount_cents": req.amount_cents,
    }

    if result.get("status") == "captured":
        # Only successful captures are memoised. Caching a decline would turn a transient
        # provider hiccup into a permanent refusal for that order.
        _captured[req.idempotency_key] = payment
        log.info(ev.PAYMENT_CAPTURED, duration_ms=t.ms, order_id=req.order_id)
    else:
        log.warn(ev.PAYMENT_DECLINED, duration_ms=t.ms, order_id=req.order_id,
                 reason=result.get("reason"))

    return payment


@app.on_event("startup")
async def _log_client_config() -> None:
    # A deliberately leaky startup line, of the kind real services emit: it prints the
    # provider credential. The value is sherLOG's canary secret. The redaction gate must
    # remove it before any model or tracer sees it, and the LLM client refuses to send a
    # payload that still contains it.
    log.info(ev.CONFIG_LOADED, "psp client configured", psp_base_url=PSP_BASE_URL,
             psp_api_key=os.environ.get("PSP_API_KEY", ""))


@app.on_event("shutdown")
async def _shutdown() -> None:
    await psp.aclose()
    log.info(ev.SERVICE_STOP, "payments-svc shutting down")
