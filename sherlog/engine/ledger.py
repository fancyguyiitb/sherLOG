"""The hypothesis ledger and the stage machine that drives it.

``ARCHITECTURE.md`` §7 rejects an open-ended ReAct loop. Unbounded loops wander, burn
budget, and produce confident nonsense; more importantly they leave no artifact anyone can
audit afterwards. What replaces it is a six-stage machine over an explicit ledger:

    Triage -> Hypothesize -> Gather -> Verify -> Critic -> Report

The highest-leverage line in the whole design is that ``predicted_signals`` are written
during Hypothesize, *before* any evidence is gathered for that hypothesis. That single
ordering constraint converts the model from a narrator — which will always find a story
that fits whatever it was shown — into a falsifier. A prediction that later checks as
absent refutes its hypothesis mechanically, and no amount of fluent prose can talk it back.

The ledger is not debug output. It is what the frontend renders and what the eval harness
scores, so it has to be persisted and queryable regardless — which is most of the reason
an orchestration framework buys us nothing here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from ..models import Assessment, Claim, Hypothesis, RedactedEvidence
from .budget import BudgetGovernor
from .outcome import Decision
from .posterior import SignalCheck, margin, update_posteriors

Stage = Literal["triage", "hypothesize", "gather", "verify", "critic", "report", "done"]

STAGE_ORDER: tuple[Stage, ...] = (
    "triage",
    "hypothesize",
    "gather",
    "verify",
    "critic",
    "report",
    "done",
)

#: Hypothesize must produce at least this many competing explanations. One hypothesis is
#: not an investigation, it is a guess with extra steps — and the margin requirement in the
#: outcome gate is meaningless without a runner-up to measure against.
MIN_HYPOTHESES = 3
MAX_HYPOTHESES = 5


class LedgerError(RuntimeError):
    pass


@dataclass
class Investigation:
    """One incident under investigation. Every field here has a column in Postgres."""

    id: str
    incident_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    stage: Stage = "triage"
    mode: Literal["live", "replay", "frozen"] = "live"
    degraded: bool = False

    hypotheses: list[Hypothesis] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)
    signal_checks: list[SignalCheck] = field(default_factory=list)
    evidence: dict[str, RedactedEvidence] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)

    critic_concurs: bool = True
    decision: Decision | None = None
    governor: BudgetGovernor = field(default_factory=BudgetGovernor)

    # --- stage transitions -------------------------------------------------------------

    def advance(self, to: Stage) -> None:
        """Stages run in order. Skipping Critic to reach a conclusion faster is exactly the
        shortcut this machine exists to prevent, so the transition is checked."""
        current_idx = STAGE_ORDER.index(self.stage)
        target_idx = STAGE_ORDER.index(to)
        if target_idx != current_idx + 1:
            raise LedgerError(f"illegal transition {self.stage} -> {to}")
        self.stage = to

    # --- ledger operations -------------------------------------------------------------

    def open_hypotheses(self, hypotheses: list[Hypothesis]) -> None:
        if not MIN_HYPOTHESES <= len(hypotheses) <= MAX_HYPOTHESES:
            raise LedgerError(
                f"Hypothesize must return {MIN_HYPOTHESES}-{MAX_HYPOTHESES} hypotheses, "
                f"got {len(hypotheses)}"
            )
        for h in hypotheses:
            if not h.predicted_signals:
                raise LedgerError(
                    f"hypothesis {h.id} registered no predicted signals; an unfalsifiable "
                    "hypothesis cannot be scored and must not enter the ledger"
                )
        self.hypotheses = hypotheses

    def attach(self, evidence: RedactedEvidence) -> str:
        """Add evidence to the store. Content addressing makes this idempotent, so two
        subagents issuing the same query cost one retrieval and produce one citation."""
        self.evidence.setdefault(evidence.id, evidence)
        return evidence.id

    def assess(self, assessment: Assessment) -> None:
        if assessment.evidence_id not in self.evidence:
            raise LedgerError(
                f"assessment cites {assessment.evidence_id}, which is not in the store"
            )
        self.assessments.append(assessment)
        target = self.get(assessment.hypothesis_id)
        bucket = (
            target.supporting
            if assessment.verdict.endswith("SUPPORT")
            else target.contradicting
            if assessment.verdict.endswith("REFUTE")
            else None
        )
        if bucket is not None and assessment.evidence_id not in bucket:
            bucket.append(assessment.evidence_id)

    def record_signal_check(self, hypothesis_id: str, signal: str, present: bool) -> None:
        """Mechanical falsification. No model in this loop — the evidence layer either
        found the pre-registered signal or it did not."""
        self.signal_checks.append(SignalCheck(hypothesis_id, signal, present))

    def get(self, hypothesis_id: str) -> Hypothesis:
        for h in self.hypotheses:
            if h.id == hypothesis_id:
                return h
        raise LedgerError(f"unknown hypothesis {hypothesis_id}")

    def recompute(self) -> list[Hypothesis]:
        self.hypotheses = update_posteriors(
            self.hypotheses, self.assessments, self.signal_checks
        )
        return self.hypotheses

    # --- readouts ----------------------------------------------------------------------

    @property
    def leader(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    @property
    def margin(self) -> float:
        return margin(self.hypotheses)

    def snapshot(self) -> dict[str, object]:
        """The payload the live investigation view streams over SSE. Hypothesis bars move
        as this changes, which is what makes the reasoning legible rather than asserted."""
        return {
            "investigation_id": self.id,
            "stage": self.stage,
            "degraded": self.degraded,
            "margin": round(self.margin, 3),
            "hypotheses": [
                {
                    "id": h.id,
                    "statement": h.statement,
                    "posterior": round(h.posterior, 3),
                    "status": h.status,
                    "supporting": list(h.supporting),
                    "contradicting": list(h.contradicting),
                    "predicted_signals": list(h.predicted_signals),
                }
                for h in self.hypotheses
            ],
            "evidence_count": len(self.evidence),
            "budget": self.governor.snapshot(),
        }
