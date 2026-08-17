"""The outcome gate — pure code, no model call.

Whether an investigation is allowed to state a conclusion is decided here, from the
posterior, the margin, the critic's verdict, the citation-verification result and the
budget. Handing that decision to a model would put the one judgment we most need to trust
back inside the thing we are trying to constrain.

``INSUFFICIENT_EVIDENCE`` is a success state. F-07 in the fault catalog exists specifically
to be answered that way, and a confident answer on it is scored as a failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Hypothesis, Outcome
from .posterior import margin

CONFIRM_POSTERIOR = 0.80
CONFIRM_MARGIN = 0.25
LIKELY_POSTERIOR = 0.55

#: Ceiling applied when some claims failed programmatic citation verification.
PARTIAL_CONFIDENCE_CAP = 0.70
#: Ceiling applied when the adversarial critic dissents from the conclusion.
CRITIC_DISSENT_CAP = 0.79


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    confidence: float
    reason: str
    top_hypothesis_id: str | None
    margin: float


def decide(
    hypotheses: list[Hypothesis],
    *,
    critic_concurs: bool,
    unverified_claims: int,
    budget_exhausted: bool = False,
    provider_unavailable: bool = False,
) -> Decision:
    """Map ledger state onto one of four outcomes.

    Order matters. Budget and provider failures short-circuit before any confidence is
    read, because a run that stopped early has no business reporting a conclusion drawn
    from evidence it never gathered.
    """
    ranked = sorted(hypotheses, key=lambda h: h.posterior, reverse=True)
    top = ranked[0] if ranked else None
    m = margin(ranked)

    if provider_unavailable:
        return Decision("INSUFFICIENT_EVIDENCE", 0.0, "provider_unavailable", None, m)

    if budget_exhausted:
        return Decision(
            "INSUFFICIENT_EVIDENCE",
            0.0,
            "budget",
            top.id if top else None,
            m,
        )

    if top is None:
        return Decision("INSUFFICIENT_EVIDENCE", 0.0, "no_hypotheses", None, m)

    confidence = top.posterior

    if unverified_claims > 0:
        return Decision(
            "PARTIALLY_VERIFIED",
            min(confidence, PARTIAL_CONFIDENCE_CAP),
            f"{unverified_claims} claim(s) failed citation verification",
            top.id,
            m,
        )

    if confidence >= CONFIRM_POSTERIOR and m >= CONFIRM_MARGIN and critic_concurs:
        return Decision("CONFIRMED", confidence, "confirmed", top.id, m)

    if confidence >= LIKELY_POSTERIOR:
        capped = min(confidence, CRITIC_DISSENT_CAP) if not critic_concurs else confidence
        reason = "critic_dissent" if not critic_concurs else "below_confirm_threshold"
        return Decision("LIKELY", capped, reason, top.id, m)

    return Decision("INSUFFICIENT_EVIDENCE", confidence, "low_confidence", top.id, m)
