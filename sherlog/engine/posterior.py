"""Posterior arithmetic — the model labels, the code computes.

LLMs judge well and count badly. Asked for a final confidence number, one produces a
fluent guess that is uncorrelated with correctness. So no model in this system ever emits
a posterior. A model emits a discrete label per (hypothesis, evidence) pair; this module
maps labels to fixed log-likelihood ratios and does the arithmetic deterministically.

Three guards, each closing a real failure mode:

*   **Deduplication** — an evidence ID counts once per hypothesis. Content addressing
    (``sherlog.models.evidence_id``) makes that exact rather than best-effort.
*   **Correlated-evidence cap** — total weight from one evidence *kind* toward one
    hypothesis is clamped to ±2.0 nats. Ten log lines saying the same thing are one fact;
    without the clamp the log tool alone drives any hypothesis to certainty. Two things
    deliberately sit *outside* the cap, because neither is correlated evidence:
    ``DECISIVE_REFUTE`` and mechanical falsification.
*   **Mechanical falsification** — a pre-registered ``predicted_signal`` that checks as
    absent costs −2.0, applied by code with no model in the loop.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from ..models import Assessment, Hypothesis, Verdict

#: Log-likelihood ratio in nats per verdict label. Fixed, not learned — with 36 scored runs
#: in v1 there is nowhere near enough data to fit these, and a fitted-on-6-faults weight
#: table would be overfitting dressed up as calibration.
WEIGHTS: dict[Verdict, float] = {
    "STRONG_SUPPORT": +1.4,
    "WEAK_SUPPORT": +0.5,
    "NEUTRAL": 0.0,
    "WEAK_REFUTE": -0.5,
    "STRONG_REFUTE": -1.4,
    "DECISIVE_REFUTE": -4.0,
}

#: Ceiling on the absolute weight any single evidence kind may contribute to one hypothesis.
KIND_CAP = 2.0

#: Penalty for a pre-registered signal the evidence layer checked and did not find.
MISSING_SIGNAL_PENALTY = -2.0


@dataclass(frozen=True, slots=True)
class SignalCheck:
    """Result of mechanically checking one pre-registered prediction."""

    hypothesis_id: str
    signal: str
    present: bool


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)  # avoids overflow for large negative x
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def accumulate_weight(
    hypothesis_id: str,
    assessments: list[Assessment],
    signal_checks: list[SignalCheck] | None = None,
) -> float:
    """Total evidence weight in nats for one hypothesis, with all three guards applied."""
    seen: set[str] = set()
    per_kind: dict[str, float] = defaultdict(float)
    decisive = 0.0

    for a in assessments:
        if a.hypothesis_id != hypothesis_id:
            continue
        if a.evidence_id in seen:  # guard 1: dedup by content address
            continue
        seen.add(a.evidence_id)

        # A decisive refutation is a falsification, not a correlated support signal, so it
        # bypasses the per-kind cap. Clamping it alongside ordinary evidence would let a
        # hypothesis that some evidence is logically incompatible with survive purely
        # because the cap absorbed the contradiction — the opposite of what the cap is for.
        if a.verdict == "DECISIVE_REFUTE":
            decisive += WEIGHTS[a.verdict]
        else:
            per_kind[a.evidence_kind] += WEIGHTS[a.verdict]

    # guard 2: clamp each kind's contribution before summing
    total = sum(max(-KIND_CAP, min(KIND_CAP, w)) for w in per_kind.values()) + decisive

    # guard 3: mechanical falsification, outside the cap — a failed prediction is not
    # "more evidence of a kind", it is the hypothesis being wrong about itself.
    for check in signal_checks or []:
        if check.hypothesis_id == hypothesis_id and not check.present:
            total += MISSING_SIGNAL_PENALTY

    return total


def update_posteriors(
    hypotheses: list[Hypothesis],
    assessments: list[Assessment],
    signal_checks: list[SignalCheck] | None = None,
) -> list[Hypothesis]:
    """Recompute every posterior from scratch and normalise across open hypotheses.

    Recomputing rather than incrementally updating means the ledger is a pure function of
    its assessments, so a run is replayable from the database with no hidden state.
    """
    if not hypotheses:
        return []

    n = len(hypotheses)
    prior_logit = _logit(1.0 / n)

    raw: list[float] = []
    for h in hypotheses:
        weight = accumulate_weight(h.id, assessments, signal_checks)
        raw.append(_sigmoid(prior_logit + weight))

    total = sum(raw)
    for h, p in zip(hypotheses, raw):
        h.posterior = (p / total) if total > 0 else 1.0 / n
        h.status = _status_for(h.posterior, h.status)

    return sorted(hypotheses, key=lambda h: h.posterior, reverse=True)


def _status_for(posterior: float, current: str) -> str:
    if current == "refuted":
        return current
    if posterior < 0.05:
        return "refuted"
    if posterior >= 0.55:
        return "supported"
    return "open"


def margin(hypotheses: list[Hypothesis]) -> float:
    """Gap between the leading hypothesis and the runner-up.

    This number is why the F-01/F-02 confusable pair is scientifically meaningful. If
    cache-TTL-collapse and Redis-eviction-storm both sit near 0.5 because the agent never
    fetched the signal that separates them, the margin fails and the system abstains
    instead of coin-flipping. Abstention there is the correct answer, and it is only
    reachable because confidence is computed rather than asserted.
    """
    if len(hypotheses) < 2:
        return 1.0 if hypotheses else 0.0
    ranked = sorted((h.posterior for h in hypotheses), reverse=True)
    return ranked[0] - ranked[1]
