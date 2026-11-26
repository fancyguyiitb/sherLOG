"""Aggregate scored runs into docs/RESULTS.md.

Every number is mean ± spread over repetitions, never a single figure (SCORING.md §6).
Degraded runs and runs with an evidence-layer error are excluded from headline numbers
and listed separately (§5).

    python -m eval.report                       # runtime/eval/results.jsonl
    python -m eval.report --in runtime/eval/dev3.jsonl --out -
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ms(xs: list[float]) -> str:
    if not xs:
        return "–"
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    return f"{m:.2f} ± {sd:.2f}"


def _pct(xs: list[bool]) -> str:
    return f"{100 * sum(xs) / len(xs):.0f}% ({sum(xs)}/{len(xs)})" if xs else "–"


def build(rows: list[dict]) -> str:
    scored = [r["score"] for r in rows]
    excluded = [s for s in scored if s.get("degraded") or (s.get("error") or "").startswith(("ConnectError", "HTTP"))]
    head = [s for s in scored if s not in excluded and s.get("outcome") != "NOT_DETECTED"]
    undetected = [s for s in scored if s.get("outcome") == "NOT_DETECTED"]
    solvable = [s for s in head if s["fault_id"] != "F-07"]
    f07 = [s for s in head if s["fault_id"] == "F-07"]

    claims = sum(s.get("claims_total") or 0 for s in head)
    stripped = sum(s.get("claims_stripped") or 0 for s in head)
    decoy_runs = [s for s in head if s.get("decoys")]

    lines = [
        "# Results",
        "",
        f"{len(scored)} runs: {len(head)} scored, {len(undetected)} not detected, "
        f"{len(excluded)} excluded (degraded / evidence-layer error).",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| RCA accuracy@1, F-01..F-06 (mean ± sd over runs) | {_ms([s['rca'] for s in solvable])} |",
        f"| Service localisation | {_pct([bool(s['proximate_correct']) for s in head])} |",
        f"| Origin localisation | {_pct([bool(s['originating_correct']) for s in head])} |",
        f"| Abstention correctness (abstain on F-07, answer on the rest) | {_pct([bool(s['abstention_correct']) for s in head])} |",
        f"| Correct abstention on F-07 | {_pct([bool(s['abstained']) for s in f07])} |",
        f"| Decoy resistance (runs with decoys) | {_pct([not s['decoy_blamed'] for s in decoy_runs])} |",
        f"| Claims stripped by citation verification | {f'{100 * stripped / claims:.1f}% ({stripped}/{claims})' if claims else '–'} |",
        f"| MTTD, injection -> detection (s) | {_ms([s['mttd_s'] for s in head if s.get('mttd_s') is not None])} |",
        f"| MTTR-diagnosis, detection -> verdict (s) | {_ms([s['mttr_diagnosis_s'] for s in head if s.get('mttr_diagnosis_s') is not None])} |",
        f"| Cost per investigation (USD) | {_ms([s['cost_usd'] for s in head])} |",
        "",
        "## Per fault",
        "",
        "| Fault | Runs | RCA@1 | Service | Origin | Outcomes |",
        "|---|---|---|---|---|---|",
    ]
    by_fault: dict[str, list[dict]] = defaultdict(list)
    for s in head:
        by_fault[s["fault_id"]].append(s)
    for f in sorted(by_fault):
        ss = by_fault[f]
        outcomes = defaultdict(int)
        for s in ss:
            outcomes[s["outcome"]] += 1
        lines.append(
            f"| {f} | {len(ss)} | {_ms([s['rca'] for s in ss])} | "
            f"{_pct([bool(s['proximate_correct']) for s in ss])} | "
            f"{_pct([bool(s['originating_correct']) for s in ss])} | "
            + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())) + " |")
    lines += ["", "## Every run", "", "| Run | Outcome | RCA | Leading hypothesis |", "|---|---|---|---|"]
    for r in rows:
        s = r["score"]
        lines.append(f"| {r['key']} | {s.get('outcome')} | {s.get('rca')} | "
                     f"{(s.get('leader') or '').replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(ROOT / "runtime" / "eval" / "results.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "RESULTS.md"))
    args = ap.parse_args()
    rows = [json.loads(line) for line in Path(args.src).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    text = build(rows)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
