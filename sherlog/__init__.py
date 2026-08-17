"""sherLOG — a verifiable AI SRE.

The thesis is not "an AI SRE". It is a *verifiable* one: an agentic system whose
correctness is continuously measured against injected ground truth, and which refuses to
answer when the evidence is insufficient.

That is only possible because we own the environment and inject the faults, so we know the
true root cause of every incident. A product deployed into a customer's production can
never say "our RCA is 84% accurate", because nobody there knows ground truth either.

Package layout mirrors the planes in ARCHITECTURE.md §2:

    sherlog.detect      Plane 3 — SLO burn-rate detection and alert correlation
    sherlog.evidence    Plane 4 — seven deterministic tools, no model anywhere
    sherlog.redaction   the gate between Plane 4 and Plane 5
    sherlog.engine      Plane 5 — hypothesis ledger, posterior arithmetic, budget, outcome
    sherlog.grounding   Plane 6 — citation verification
    sherlog.llm         the single seam through which every model call passes
"""

__version__ = "0.1.0"
