"""Programmatic citation verification — the headline guardrail.

Every claim in a report carries the IDs of the evidence that licenses it. Four mechanical
checks run against each claim. None of them involves a model: using an LLM to check an LLM
as the primary defence just moves the failure one level up.

============================  ==========================================================
Check                         Catches
============================  ==========================================================
``MISSING_EVIDENCE``          Cited IDs that are not in this investigation's store —
                              i.e. fabricated citations.
``TEMPORAL_MISMATCH``         A claimed timestamp outside every cited window.
``NUMBER_NOT_FOUND``          A figure in the claim that appears in no cited evidence,
                              within tolerance — invented numbers.
``ENTITY_NOT_FOUND``          A service, commit or component named in the claim but
                              absent from the cited evidence.
============================  ==========================================================

Failing claims are stripped from the report and the investigation is marked
``PARTIALLY_VERIFIED``. The strip-rate is logged and reported: "3.2% of generated claims
failed citation verification and were removed" is a measurement, where "we prompt it not
to hallucinate" is a hope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from ..models import Claim, RedactedEvidence

FailureKind = Literal[
    "MISSING_EVIDENCE",
    "NO_CITATION",
    "TEMPORAL_MISMATCH",
    "NUMBER_NOT_FOUND",
    "ENTITY_NOT_FOUND",
]

#: Relative tolerance when matching a number in a claim against the cited evidence.
#: Non-zero because a report legitimately rounds "1483ms" to "~1.5s"; small enough that
#: an invented figure does not slip through.
NUMBER_TOLERANCE = 0.05

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class Failure:
    kind: FailureKind
    detail: str


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    claim: Claim
    failures: tuple[Failure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class VerificationReport:
    verified: tuple[Claim, ...]
    stripped: tuple[ClaimVerification, ...]

    @property
    def total(self) -> int:
        return len(self.verified) + len(self.stripped)

    @property
    def strip_rate(self) -> float:
        return len(self.stripped) / self.total if self.total else 0.0


def _numbers_in(text: str) -> list[float]:
    return [float(m.group(0)) for m in _NUMBER_RE.finditer(text)]


def _number_present(target: float, haystack: list[float]) -> bool:
    for value in haystack:
        if target == value:
            return True
        scale = max(abs(target), abs(value), 1e-9)
        if abs(target - value) / scale <= NUMBER_TOLERANCE:
            return True
    return False


def verify_claim(
    claim: Claim, store: dict[str, RedactedEvidence]
) -> ClaimVerification:
    failures: list[Failure] = []

    if not claim.evidence_ids:
        return ClaimVerification(claim, (Failure("NO_CITATION", "claim cites nothing"),))

    cited: list[RedactedEvidence] = []
    for eid in claim.evidence_ids:
        ev = store.get(eid)
        if ev is None:
            failures.append(Failure("MISSING_EVIDENCE", f"{eid} not in evidence store"))
        else:
            cited.append(ev)

    if not cited:
        return ClaimVerification(claim, tuple(failures))

    if claim.timestamp is not None and not any(ev.covers(claim.timestamp) for ev in cited):
        failures.append(
            Failure(
                "TEMPORAL_MISMATCH",
                f"{claim.timestamp.isoformat()} outside every cited window",
            )
        )

    corpus = " ".join(ev.content for ev in cited)
    corpus_numbers = _numbers_in(corpus)
    for number in claim.numbers:
        if not _number_present(number, corpus_numbers):
            failures.append(Failure("NUMBER_NOT_FOUND", f"{number:g} absent from cited evidence"))

    lowered = corpus.lower()
    for entity in claim.entities:
        if entity.lower() not in lowered:
            failures.append(Failure("ENTITY_NOT_FOUND", f"'{entity}' absent from cited evidence"))

    return ClaimVerification(claim, tuple(failures))


def verify_all(
    claims: Iterable[Claim], store: dict[str, RedactedEvidence]
) -> VerificationReport:
    verified: list[Claim] = []
    stripped: list[ClaimVerification] = []
    for claim in claims:
        result = verify_claim(claim, store)
        if result.ok:
            verified.append(claim)
        else:
            stripped.append(result)
    return VerificationReport(tuple(verified), tuple(stripped))
