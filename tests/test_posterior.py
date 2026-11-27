"""The arithmetic the model is not allowed to do.

Each guard here corresponds to a specific way a confidence number gets inflated, and each
test is written to fail if that guard is removed.
"""

from __future__ import annotations

import pytest

from sherlog.engine.posterior import (
    KIND_CAP,
    MISSING_SIGNAL_PENALTY,
    SignalCheck,
    accumulate_weight,
    margin,
    update_posteriors,
)
from sherlog.models import Assessment, Hypothesis


def h(hid: str, statement: str = "x") -> Hypothesis:
    return Hypothesis(id=hid, statement=statement, predicted_signals=["s"])


def a(hid: str, eid: str, verdict: str, kind: str = "log") -> Assessment:
    return Assessment(hid, eid, kind, verdict, "because")


def test_posteriors_sum_to_one():
    hyps = [h("H1"), h("H2"), h("H3")]
    result = update_posteriors(hyps, [a("H1", "ev_1", "STRONG_SUPPORT")])
    assert sum(x.posterior for x in result) == pytest.approx(1.0)


def test_support_raises_and_refutation_lowers():
    hyps = [h("H1"), h("H2"), h("H3")]
    result = update_posteriors(
        hyps,
        [a("H1", "ev_1", "STRONG_SUPPORT"), a("H2", "ev_2", "STRONG_REFUTE")],
    )
    by_id = {x.id: x.posterior for x in result}
    assert by_id["H1"] > by_id["H3"] > by_id["H2"]


def test_decisive_refutation_effectively_eliminates():
    hyps = [h("H1"), h("H2"), h("H3")]
    result = update_posteriors(hyps, [a("H1", "ev_1", "DECISIVE_REFUTE")])
    refuted = next(x for x in result if x.id == "H1")
    assert refuted.posterior < 0.05
    assert refuted.status == "refuted"


# --- guard 1: deduplication ----------------------------------------------------------------

def test_the_same_evidence_counts_once():
    """Content addressing makes this exact.

    Two subagents issuing the same query produce the same evidence ID. Without dedup, a
    hypothesis gains confidence purely from how many subagents happened to look at it.
    """
    once = accumulate_weight("H1", [a("H1", "ev_dup", "STRONG_SUPPORT")])
    twice = accumulate_weight(
        "H1",
        [a("H1", "ev_dup", "STRONG_SUPPORT"), a("H1", "ev_dup", "STRONG_SUPPORT")],
    )
    assert once == twice == pytest.approx(1.4)


# --- guard 2: correlated-evidence cap -------------------------------------------------------

def test_one_evidence_kind_cannot_drive_a_hypothesis_to_certainty():
    """Ten log lines saying the same thing are one fact.

    Without the clamp the log tool alone reaches +14 nats, which is a posterior of
    essentially 1.0 from a single signal type. That is how an agent becomes certain about
    something it looked at from exactly one angle.
    """
    many_logs = [a("H1", f"ev_{i}", "STRONG_SUPPORT", "log") for i in range(10)]
    assert accumulate_weight("H1", many_logs) == pytest.approx(KIND_CAP)


def test_corroboration_across_kinds_is_rewarded():
    """The cap is per-kind, so independent signals still accumulate — which is the
    behaviour we want: a metric and a trace agreeing is genuinely stronger than two logs."""
    cross = [
        a("H1", "ev_1", "STRONG_SUPPORT", "log"),
        a("H1", "ev_2", "STRONG_SUPPORT", "metric"),
        a("H1", "ev_3", "STRONG_SUPPORT", "trace"),
    ]
    assert accumulate_weight("H1", cross) == pytest.approx(4.2)


# --- guard 3: mechanical falsification -------------------------------------------------------

def test_a_missing_predicted_signal_is_penalised_without_a_model():
    """The hypothesis ledger's whole point, made operational.

    The agent pre-registered what must be true. Code checked, and it was not. No amount of
    fluent narration gets past this, because no model is consulted.
    """
    supported = [a("H1", "ev_1", "STRONG_SUPPORT")]
    with_missing = accumulate_weight(
        "H1", supported, [SignalCheck("H1", "db_pool_waiters > 10", present=False)]
    )
    assert with_missing == pytest.approx(1.4 + MISSING_SIGNAL_PENALTY)


def test_falsification_is_not_subject_to_the_kind_cap():
    """A failed prediction is not "more evidence of a kind" — it is the hypothesis being
    wrong about itself, so capping it alongside evidence would let a well-corroborated but
    falsified hypothesis survive."""
    logs = [a("H1", f"ev_{i}", "STRONG_SUPPORT", "log") for i in range(10)]
    checks = [
        SignalCheck("H1", "signal_a", present=False),
        SignalCheck("H1", "signal_b", present=False),
    ]
    assert accumulate_weight("H1", logs, checks) == pytest.approx(KIND_CAP + 2 * MISSING_SIGNAL_PENALTY)


# --- margin --------------------------------------------------------------------------------

def test_the_confusable_pair_produces_a_thin_margin():
    """F-01 vs F-02, when the agent gathered only the confirming signal.

    Cache misses are up — true for both. Both hypotheses get identical support, the margin
    collapses, and the outcome gate abstains rather than coin-flipping. That abstention is
    the correct answer, and it is only reachable because confidence is computed.
    """
    hyps = [h("H_ttl"), h("H_eviction"), h("H_other")]
    both_supported = [
        a("H_ttl", "ev_miss", "STRONG_SUPPORT", "metric"),
        a("H_eviction", "ev_miss2", "STRONG_SUPPORT", "metric"),
    ]
    result = update_posteriors(hyps, both_supported)
    assert margin(result) < 0.25


def test_the_discriminating_signal_opens_the_margin():
    """The same pair once the agent fetches redis_evicted_keys_total: flat refutes eviction,
    which is the evidence that actually separates them."""
    hyps = [h("H_ttl"), h("H_eviction"), h("H_other")]
    discriminated = [
        a("H_ttl", "ev_miss", "STRONG_SUPPORT", "metric"),
        a("H_ttl", "ev_confighash", "STRONG_SUPPORT", "state"),
        a("H_eviction", "ev_evictions_flat", "STRONG_REFUTE", "metric"),
    ]
    result = update_posteriors(hyps, discriminated)
    assert result[0].id == "H_ttl"
    assert margin(result) >= 0.25


def test_residual_hypothesis_stops_normalisation_manufacturing_confidence():
    """Every candidate mechanically refuted: without the residual the least-bad one is
    renormalised to near-certainty; with it, the mass goes to 'none of the above'."""
    from sherlog.engine.posterior import SignalCheck

    hyps = [Hypothesis(f"H{i}", "s", ["x"]) for i in range(1, 4)]
    checks = [SignalCheck("H1", "a", False),
              SignalCheck("H2", "a", False), SignalCheck("H2", "b", False),
              SignalCheck("H3", "a", False), SignalCheck("H3", "b", False)]
    plain = update_posteriors([Hypothesis(h.id, "s", ["x"]) for h in hyps], [], checks)
    assert plain[0].posterior > 0.7
    honest = update_posteriors(hyps, [], checks, residual=True)
    assert honest[0].posterior < 0.55


def test_two_well_supported_rivals_are_not_squashed_into_a_tie():
    """BUILD_LOG O1 (matrix 2, F-02 rep1): the cause collected +10.1 nats, a symptom-level
    rival +6.4. Squashing each through its own sigmoid before normalising put both near
    1.0 and produced 0.456 vs 0.453. Bayes over exclusive hypotheses keeps the 3.7-nat gap
    as a ~40:1 ratio."""
    kinds = ["metric", "state", "trace", "log", "git", "doc"]
    cause = [a("H1", f"c{i}", "STRONG_SUPPORT", k) for i, k in enumerate(kinds * 2)]
    symptom = [a("H2", f"s{i}", "STRONG_SUPPORT", k) for i, k in enumerate(["metric", "git"] * 2)]
    symptom += [a("H2", "s9", "STRONG_SUPPORT", "state"), a("H2", "s10", "WEAK_SUPPORT", "trace")]
    result = update_posteriors([h("H1"), h("H2"), h("H3")], cause + symptom, residual=True)
    assert result[0].id == "H1"
    assert margin(result) > 0.9


def test_equal_support_still_ties():
    """The fix must not invent a winner: identical evidence weight is still a coin flip."""
    both = [a("H1", "e1", "STRONG_SUPPORT", "metric"), a("H2", "e2", "STRONG_SUPPORT", "metric")]
    result = update_posteriors([h("H1"), h("H2"), h("H3")], both, residual=True)
    assert margin(result) == pytest.approx(0.0)
