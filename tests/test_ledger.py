"""The hypothesis ledger and the budget governor.

The ledger's invariants are what separate this from a ReAct loop: hypotheses must be
plural, they must be falsifiable before evidence is gathered, and stages cannot be skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sherlog.engine.budget import Budget, BudgetExhausted, BudgetGovernor
from sherlog.engine.ledger import Investigation, LedgerError
from sherlog.models import Assessment, Evidence, Hypothesis
from sherlog.redaction import redact

T0 = datetime(2026, 8, 15, 14, 30, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)


def _inv() -> Investigation:
    return Investigation(id="inv_1", incident_id="inc_1")


def _hyps(n: int = 3) -> list[Hypothesis]:
    return [
        Hypothesis(id=f"H{i}", statement=f"hypothesis {i}", predicted_signals=[f"signal_{i}"])
        for i in range(n)
    ]


def _evidence(query: str = "q"):
    return redact(Evidence("metric", query, T0, T1, "db_pool_waiters peaked at 14", "prom"))


# --- ledger invariants ------------------------------------------------------------------

def test_a_hypothesis_without_predictions_is_rejected():
    """The highest-leverage constraint in the design.

    An unfalsifiable hypothesis cannot be scored, and admitting one turns the model back
    into a narrator that will always find a story fitting whatever it was shown.
    """
    inv = _inv()
    bad = _hyps(3)
    bad[1].predicted_signals = []
    with pytest.raises(LedgerError, match="predicted signals"):
        inv.open_hypotheses(bad)


def test_a_single_hypothesis_is_not_an_investigation():
    """One hypothesis is a guess with extra steps, and the outcome gate's margin
    requirement is meaningless without a runner-up to measure against."""
    with pytest.raises(LedgerError):
        _inv().open_hypotheses(_hyps(1))


def test_too_many_hypotheses_are_rejected():
    with pytest.raises(LedgerError):
        _inv().open_hypotheses(_hyps(9))


def test_stages_cannot_be_skipped():
    """Skipping Critic to reach a conclusion faster is exactly the shortcut the state
    machine exists to prevent."""
    inv = _inv()
    inv.advance("hypothesize")
    with pytest.raises(LedgerError, match="illegal transition"):
        inv.advance("report")


def test_an_assessment_citing_absent_evidence_is_rejected():
    inv = _inv()
    inv.open_hypotheses(_hyps())
    with pytest.raises(LedgerError, match="not in the store"):
        inv.assess(Assessment("H0", "ev_ffffffff", "metric", "STRONG_SUPPORT", "why"))


def test_attaching_the_same_evidence_twice_is_idempotent():
    """Content addressing means two subagents issuing one query cost one retrieval and
    produce one citation."""
    inv = _inv()
    first = inv.attach(_evidence())
    second = inv.attach(_evidence())
    assert first == second
    assert len(inv.evidence) == 1


def test_assessments_populate_the_supporting_and_contradicting_lists():
    inv = _inv()
    inv.open_hypotheses(_hyps())
    eid = inv.attach(_evidence())
    inv.assess(Assessment("H0", eid, "metric", "STRONG_SUPPORT", "waiters elevated"))
    inv.assess(Assessment("H1", eid, "metric", "STRONG_REFUTE", "pool size unchanged"))
    assert inv.get("H0").supporting == [eid]
    assert inv.get("H1").contradicting == [eid]


def test_the_snapshot_is_renderable():
    """The ledger is not debug output — it is what the frontend streams and what the eval
    harness scores, so it has to be complete and serialisable."""
    inv = _inv()
    inv.open_hypotheses(_hyps())
    eid = inv.attach(_evidence())
    inv.assess(Assessment("H0", eid, "metric", "STRONG_SUPPORT", "why"))
    inv.record_signal_check("H1", "signal_1", present=False)
    inv.recompute()

    snap = inv.snapshot()
    assert snap["stage"] == "triage"
    assert len(snap["hypotheses"]) == 3
    assert snap["hypotheses"][0]["id"] == "H0"
    assert "budget" in snap and "cost_usd" in snap["budget"]


def test_a_failed_prediction_demotes_its_hypothesis():
    inv = _inv()
    inv.open_hypotheses(_hyps())
    eid = inv.attach(_evidence())
    inv.assess(Assessment("H1", eid, "metric", "WEAK_SUPPORT", "loosely consistent"))
    inv.record_signal_check("H1", "signal_1", present=False)
    ranked = inv.recompute()
    assert ranked[-1].id == "H1"


# --- budget governor -----------------------------------------------------------------------

def test_the_governor_stops_the_run_before_the_next_call():
    """Checked before spending, not after. Checking afterwards means the cap is always
    breached by exactly one call — which for a reasoning-tier call is the expensive one."""
    gov = BudgetGovernor(Budget(max_tokens=1000))
    gov.charge_llm(600, 400, 0.10)
    with pytest.raises(BudgetExhausted, match="tokens"):
        gov.check()


def test_the_dollar_cap_is_enforced():
    gov = BudgetGovernor(Budget(max_cost_usd=0.60))
    gov.charge_llm(1000, 500, 0.61)
    with pytest.raises(BudgetExhausted, match="cost_usd"):
        gov.check()


def test_the_tool_call_cap_is_enforced():
    gov = BudgetGovernor(Budget(max_tool_calls=3))
    for _ in range(3):
        gov.charge_tool()
    with pytest.raises(BudgetExhausted, match="tool_calls"):
        gov.charge_tool()


def test_wall_clock_is_enforced_with_an_injected_clock():
    ticks = iter([0.0, 100.0, 500.0])
    gov = BudgetGovernor(Budget(max_wall_clock_s=480.0), clock=lambda: next(ticks))
    gov.check()  # 100s elapsed
    with pytest.raises(BudgetExhausted, match="wall_clock"):
        gov.check()  # 500s elapsed


def test_the_snapshot_reports_percentages_for_the_ui():
    """Cost-awareness on screen reads as production maturity, and it is only possible
    because the governor tracks spend per investigation rather than per month."""
    gov = BudgetGovernor()
    gov.charge_llm(100_000, 25_000, 0.30)
    snap = gov.snapshot()
    assert snap["tokens"] == 125_000
    assert snap["cost_pct"] == 50.0
