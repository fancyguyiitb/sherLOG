"""Re-apply the posterior arithmetic and outcome gate to stored investigations.

Offline, no model calls. The posterior only feeds the outcome gate and the critic/report
prompts (it never steers which evidence is gathered), so recomputing it from a stored
ledger's assessments and signal checks is exact for the outcome; the stored critic and
report text were written against the old numbers and are reused as-is.

    python -m eval.rescore                        # runtime/eval/results.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.run import fetch_injection
from eval.score import score
from sherlog.engine.outcome import decide
from sherlog.engine.posterior import SignalCheck, update_posteriors
from sherlog.engine.run import unobservable
from sherlog.models import Assessment, Hypothesis

ROOT = Path(__file__).resolve().parents[1]


def recompute(inv: dict) -> dict:
    hyps = [Hypothesis(h["id"], h["statement"], list(h.get("predicted_signals") or ["-"]))
            for h in inv["ledger"]["hypotheses"]]
    assessments = [Assessment(a["hypothesis_id"], a["evidence_id"], a["evidence_kind"],
                              a["verdict"], a.get("rationale", "")) for a in inv["assessments"]]
    checks = [SignalCheck(c["hypothesis_id"], c["signal"], c["present"]) for c in inv["signal_checks"]]
    ranked = update_posteriors(hyps, assessments, checks, residual=True)
    critic = inv.get("critic") or {}
    concurs = critic.get("verdict", "CONCUR") == "CONCUR" or bool(critic.get("overruled"))
    leader_comp = inv["hypotheses_meta"].get(ranked[0].id, {}).get("component")
    v = inv.get("verification") or {}
    d = decide(ranked, critic_concurs=concurs, unverified_claims=v.get("stripped", 0),
               verified_claims=v.get("verified") if v else None,
               budget_exhausted=(inv.get("error") or "").startswith("budget"),
               leader_unobservable=unobservable(leader_comp, inv.get("evidence_gaps") or []))
    out = dict(inv)
    out["ledger"] = {**inv["ledger"], "hypotheses": [
        {**next(x for x in inv["ledger"]["hypotheses"] if x["id"] == h.id), "posterior": h.posterior}
        for h in ranked]}
    out["decision"] = {"outcome": d.outcome, "confidence": d.confidence, "reason": d.reason,
                       "top_hypothesis_id": d.top_hypothesis_id, "margin": d.margin}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(ROOT / "runtime" / "eval" / "results.jsonl"))
    args = ap.parse_args()
    for line in Path(args.src).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        old = r["score"]
        if not r.get("investigation_id"):
            print(f"{r['key']:24} {old['outcome']:22} (no investigation)")
            continue
        inv = json.loads((ROOT / "runtime" / "investigations" / f"{r['investigation_id']}.json")
                         .read_text(encoding="utf-8"))
        new_inv = recompute(inv)
        new = score(new_inv, fetch_injection(r["injection_id"]))
        flip = "  <-- leader changed" if new_inv["ledger"]["hypotheses"][0]["id"] != inv["ledger"]["hypotheses"][0]["id"] else ""
        mark = "" if (old["outcome"], old["rca"]) == (new["outcome"], new["rca"]) else "  *"
        print(f"{r['key']:24} {old['outcome']:22} {old['rca']:.1f} -> {new['outcome']:22} {new['rca']:.1f} "
              f"conf {new['confidence']:.2f} margin {new_inv['decision']['margin']:.2f}{mark}{flip}")


if __name__ == "__main__":
    main()
