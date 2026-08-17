"""The outcome gate. Whether the system is allowed to state a conclusion.

INSUFFICIENT_EVIDENCE is a success state throughout these tests. A system that never says
"I don't know" is a system that hallucinates, and any competent reviewer knows it.
"""

from __future__ import annotations

import pytest

from sherlog.engine.outcome import (
    CRITIC_DISSENT_CAP,
    PARTIAL_CONFIDENCE_CAP,
    decide,
)
from sherlog.models import Hypothesis


def hyp(hid: str, posterior: float) -> Hypothesis:
    return Hypothesis(id=hid, statement=hid, predicted_signals=["s"], posterior=posterior)


def test_confirmed_needs_confidence_margin_and_a_concurring_critic():
    d = decide(
        [hyp("H1", 0.86), hyp("H2", 0.10), hyp("H3", 0.04)],
        critic_concurs=True,
        unverified_claims=0,
    )
    assert d.outcome == "CONFIRMED"
    assert d.top_hypothesis_id == "H1"


def test_high_confidence_with_a_thin_margin_does_not_confirm():
    """The F-01 / F-02 case. Two explanations near-tied is not a conclusion, however
    confident the leader looks in isolation."""
    d = decide(
        [hyp("H1", 0.46), hyp("H2", 0.44), hyp("H3", 0.10)],
        critic_concurs=True,
        unverified_claims=0,
    )
    assert d.outcome == "INSUFFICIENT_EVIDENCE"
    assert d.reason == "low_confidence"


def test_a_dissenting_critic_caps_confidence_and_blocks_confirmation():
    d = decide(
        [hyp("H1", 0.91), hyp("H2", 0.05), hyp("H3", 0.04)],
        critic_concurs=False,
        unverified_claims=0,
    )
    assert d.outcome == "LIKELY"
    assert d.confidence == pytest.approx(CRITIC_DISSENT_CAP)
    assert d.reason == "critic_dissent"


def test_failed_citation_verification_downgrades_and_caps():
    """A report containing claims that failed verification is not a confirmed diagnosis,
    no matter how strong the posterior — the number was computed over evidence that partly
    turned out not to say what the model said it said."""
    d = decide(
        [hyp("H1", 0.95), hyp("H2", 0.03), hyp("H3", 0.02)],
        critic_concurs=True,
        unverified_claims=2,
    )
    assert d.outcome == "PARTIALLY_VERIFIED"
    assert d.confidence == pytest.approx(PARTIAL_CONFIDENCE_CAP)


def test_budget_exhaustion_never_produces_a_guess():
    """Exceeding budget exits with partial findings. The alternative — answering anyway
    from whatever evidence happened to be gathered before the money ran out — is precisely
    the behaviour the governor exists to prevent."""
    d = decide(
        [hyp("H1", 0.99), hyp("H2", 0.01)],
        critic_concurs=True,
        unverified_claims=0,
        budget_exhausted=True,
    )
    assert d.outcome == "INSUFFICIENT_EVIDENCE"
    assert d.reason == "budget"


def test_provider_unavailability_is_distinguishable_from_low_confidence():
    """A run that could not reach a model is a different failure from a run that reached
    one and remained unconvinced. Collapsing them would corrupt the abstention metric."""
    d = decide([hyp("H1", 0.99)], critic_concurs=True, unverified_claims=0,
               provider_unavailable=True)
    assert d.outcome == "INSUFFICIENT_EVIDENCE"
    assert d.reason == "provider_unavailable"


def test_f07_shape_abstains():
    """F-07: metrics show payments-svc degraded, no logs or traces exist to say why.

    Every hypothesis stays weak because nothing can be checked. Abstention here is the
    scored-correct answer and a confident one is scored as a failure.
    """
    d = decide(
        [hyp("H1", 0.34), hyp("H2", 0.33), hyp("H3", 0.33)],
        critic_concurs=True,
        unverified_claims=0,
    )
    assert d.outcome == "INSUFFICIENT_EVIDENCE"


def test_likely_is_reachable_between_the_thresholds():
    d = decide(
        [hyp("H1", 0.65), hyp("H2", 0.20), hyp("H3", 0.15)],
        critic_concurs=True,
        unverified_claims=0,
    )
    assert d.outcome == "LIKELY"
    assert d.confidence == pytest.approx(0.65)
