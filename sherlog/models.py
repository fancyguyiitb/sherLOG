"""Core datatypes shared across every plane.

Two invariants live here and are enforced structurally rather than by convention:

1.  **Evidence IDs are content addresses.** ``id = 'ev_' + sha256(kind|query|t_start|t_end)[:8]``
    Two subagents issuing the same query against the same window get the *same* ID, so
    citations are stable, the retrieval cache is automatic, and the citation verifier's
    existence check is sound.

2.  **``RedactedEvidence`` cannot be constructed outside the redaction gate.** It requires
    the private ``_GATE_KEY`` sentinel. ``LLMClient.complete()`` accepts nothing else, which
    makes the §9 chokepoint a type boundary instead of a code-review rule.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

EvidenceKind = Literal["log", "metric", "trace", "git", "state", "doc"]

Verdict = Literal[
    "STRONG_SUPPORT",
    "WEAK_SUPPORT",
    "NEUTRAL",
    "WEAK_REFUTE",
    "STRONG_REFUTE",
    "DECISIVE_REFUTE",
]

Outcome = Literal["CONFIRMED", "LIKELY", "PARTIALLY_VERIFIED", "INSUFFICIENT_EVIDENCE"]

HypothesisStatus = Literal["open", "supported", "refuted", "insufficient"]


class GateViolation(RuntimeError):
    """Raised when something tries to build redacted evidence outside the gate."""


# Module-private sentinel. ``sherlog.redaction`` is the only intended importer; any other
# construction path raises GateViolation. tests/test_redaction.py asserts this.
_GATE_KEY = object()


def evidence_id(kind: str, query: str, t_start: datetime, t_end: datetime) -> str:
    """Content address for a retrieval. Deterministic across processes and runs."""
    payload = "|".join(
        [kind, query, _iso(t_start), _iso(t_end)]
    ).encode("utf-8")
    return "ev_" + hashlib.sha256(payload).hexdigest()[:8]


def _iso(ts: datetime) -> str:
    """Normalise to UTC ISO-8601 so identical instants always hash identically."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class Evidence:
    """A deterministic tool return. Never reaches a model in this form."""

    kind: EvidenceKind
    query: str
    t_start: datetime
    t_end: datetime
    content: str
    source_ref: str
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def id(self) -> str:
        return evidence_id(self.kind, self.query, self.t_start, self.t_end)

    @property
    def raw_hash(self) -> str:
        """Hash of *pre*-redaction content, kept for audit. Never sent anywhere."""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def covers(self, ts: datetime) -> bool:
        """Whether this evidence's window contains an instant — the temporal check
        behind claim verification."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return self.t_start <= ts <= self.t_end


@dataclass(frozen=True, slots=True)
class RedactedEvidence:
    """The only shape of evidence an LLM may see.

    Construct via ``sherlog.redaction.redact()``. Direct construction raises.
    """

    id: str
    kind: EvidenceKind
    query: str
    t_start: datetime
    t_end: datetime
    content: str
    source_ref: str
    raw_hash: str
    redactions: tuple[str, ...]
    _key: object = None

    def __post_init__(self) -> None:
        if self._key is not _GATE_KEY:
            raise GateViolation(
                "RedactedEvidence is constructible only by sherlog.redaction.redact(); "
                "routing around the gate would put unredacted content in a prompt."
            )

    def covers(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return self.t_start <= ts <= self.t_end


@dataclass
class Hypothesis:
    """A candidate explanation with its falsifiable predictions registered up front.

    ``predicted_signals`` is written during Hypothesize, *before* any evidence for this
    hypothesis is gathered. A pre-registered signal that later checks as absent costs the
    hypothesis a fixed penalty applied by code, with no model in the loop — the agent
    cannot narrate its way past a failed prediction.
    """

    id: str
    statement: str
    predicted_signals: list[str] = field(default_factory=list)
    supporting: list[str] = field(default_factory=list)
    contradicting: list[str] = field(default_factory=list)
    status: HypothesisStatus = "open"
    posterior: float = 0.0


@dataclass(frozen=True, slots=True)
class Assessment:
    """One model judgment: does this evidence support this hypothesis?

    The model emits a discrete label and one sentence. It never emits a number — see
    ``sherlog.engine.posterior`` for why.
    """

    hypothesis_id: str
    evidence_id: str
    evidence_kind: EvidenceKind
    verdict: Verdict
    rationale: str


@dataclass(frozen=True, slots=True)
class Claim:
    """A sentence in the final report, bound to the evidence that licenses it."""

    text: str
    evidence_ids: tuple[str, ...]
    timestamp: datetime | None = None
    numbers: tuple[float, ...] = ()
    entities: tuple[str, ...] = ()
