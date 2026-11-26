"""The eval matrix: 6 faults x 3 repetitions x 2 decoy conditions = 36 scored runs, plus
3 runs of the abstention case (F-07).

Each run is the demo's code path end to end, with nobody in the loop:

    reset -> wait for a clean baseline -> inject -> detector fires on its own
          -> investigate -> score against sherlog_truth -> reset

Results append to runtime/eval/results.jsonl, one line per run; the runner resumes by
skipping keys already present, so an interrupted overnight run loses at most one run.

    python -m eval.run                 # the full 39-run matrix
    python -m eval.run --faults F-03 --reps 1 --decoys on
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from faults.injector import mechanisms
from faults.schema import load_catalog
from sherlog.detect.service import healthy, wait_for_incident
from sherlog.engine.run import investigate
from sherlog.llm.client import OpenAIClient

from .score import score

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "runtime" / "eval" / "results.jsonl"
FAULTS = ["F-01", "F-02", "F-03", "F-04", "F-05", "F-06"]

#: The baseline window must start after the previous run's reset has settled, or the
#: comparison is against a faulted system. 5m window + 30s gap + 5m baseline, minus the
#: detection delay, plus warm-up.
COOLDOWN_S = 8 * 60
DETECT_TIMEOUT_S = 8 * 60

#: The engine commit every result was produced by.
COMMIT = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=7", "HEAD"],
                        capture_output=True, text=True).stdout.strip()


def matrix(faults: list[str], reps: int, decoy_modes: list[bool], abstain_reps: int) -> list[dict]:
    runs = []
    for rep in range(1, reps + 1):
        for decoys in decoy_modes:
            for f in faults:
                runs.append({"fault": f, "rep": rep, "decoys": decoys})
        if abstain_reps >= rep:
            runs.append({"fault": "F-07", "rep": rep, "decoys": False})
    return runs


def key(r: dict) -> str:
    return f"{r['fault']}|rep{r['rep']}|decoys={'on' if r['decoys'] else 'off'}"


def done_keys() -> set[str]:
    if not RESULTS.exists():
        return set()
    return {json.loads(line)["key"] for line in RESULTS.read_text(encoding="utf-8").splitlines()
            if line.strip()}


def fetch_injection(injection_id: int) -> dict:
    with psycopg.connect(mechanisms.TRUTH_DSN, autocommit=True) as conn:
        row = conn.execute("SELECT fault_id, injected_at, decoys, ground_truth FROM injections "
                           "WHERE id = %s", (injection_id,)).fetchone()
    assert row
    return {"fault_id": row[0], "injected_at": row[1].isoformat(), "decoys": row[2],
            "ground_truth": row[3]}


def wait_clean(since_reset: float) -> None:
    while time.monotonic() - since_reset < COOLDOWN_S or not healthy():
        time.sleep(15)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--faults", default=",".join(FAULTS))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--decoys", choices=["both", "on", "off"], default="both")
    ap.add_argument("--abstain-reps", type=int, default=3)
    ap.add_argument("--flags", default="", help="ablations, e.g. signal_checks=off,critic=off")
    ap.add_argument("--out", default="", help="results file (default runtime/eval/results.jsonl)")
    args = ap.parse_args()
    global RESULTS
    if args.out:
        RESULTS = Path(args.out)

    flags = {k: v == "on" for k, v in (kv.split("=") for kv in args.flags.split(",") if kv)}
    modes = {"both": [True, False], "on": [True], "off": [False]}[args.decoys]
    faults = [f for f in args.faults.split(",") if f]
    plan = matrix([f for f in faults if f != "F-07"], args.reps, modes,
                  args.abstain_reps if "F-07" in faults or args.faults == ",".join(FAULTS) else 0)
    catalog = load_catalog(ROOT / "faults" / "catalog")
    llm = OpenAIClient()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    suffix = ("|" + args.flags) if args.flags else ""
    done = done_keys()

    reset_at = reset_with_retry()
    errors_file = RESULTS.with_name(RESULTS.stem + ".errors.jsonl")
    # Up to three passes: a run that hit a harness failure or an unreachable model
    # provider produced no result about the engine, so it is retried rather than scored.
    for pass_no in range(1, 4):
        done = done_keys()
        todo = [r for r in plan if key(r) + suffix not in done]
        if not todo:
            break
        if pass_no > 1:
            print(f"retry pass {pass_no}: {len(todo)} run(s)", flush=True)
        for i, r in enumerate(todo, 1):
            k = key(r) + suffix
            print(f"[{i}/{len(todo)}] {k}: waiting for a clean baseline", flush=True)
            try:
                line = run_one(k, r, catalog, llm, flags, reset_at)
                if (line["score"].get("error") or "").startswith("all rungs failed"):
                    raise RuntimeError(f"provider unavailable: {line['score']['error']}")
                with RESULTS.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line, default=str) + "\n")
            except Exception as exc:  # not a result about the engine: logged, retried
                print(f"  HARNESS_ERROR {type(exc).__name__}: {exc}", flush=True)
                with errors_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"key": k, "pass": pass_no,
                                         "error": f"{type(exc).__name__}: {exc}",
                                         "at": datetime.now(timezone.utc).isoformat()}) + "\n")
            reset_at = reset_with_retry()


def run_one(k: str, r: dict, catalog: dict, llm: OpenAIClient, flags: dict,
            reset_at: float) -> dict:
    wait_clean(reset_at)
    fault = catalog[r["fault"]]
    decoys = [catalog[d] for d in fault.decoys] if r["decoys"] else []
    rec, injection_id = mechanisms.inject(fault, decoys)
    print(f"  injected at {rec.injected_at:%H:%M:%S}", flush=True)

    incident = wait_for_incident(DETECT_TIMEOUT_S)
    line: dict = {"key": k, "run": r, "injection_id": injection_id, "commit": COMMIT,
                  "at": datetime.now(timezone.utc).isoformat()}
    if incident is None:
        line["score"] = {"fault_id": r["fault"], "outcome": "NOT_DETECTED", "rca": 0.0,
                         "decoys": [d.id for d in decoys]}
        print("  not detected within timeout", flush=True)
        return line
    run = investigate(incident, llm, flags=flags, label=k)
    s = score(run.to_dict(), fetch_injection(injection_id))
    line["score"] = s
    line["investigation_id"] = run.id
    print(f"  {s['outcome']} rca={s['rca']} proximate={s['proximate_correct']} "
          f"origin={s['originating_correct']} decoy_blamed={s['decoy_blamed']} "
          f"mttd={s['mttd_s']:.0f}s cost=${s['cost_usd']:.3f} :: {s['leader']}", flush=True)
    return line


def reset_with_retry() -> float:
    """A failed reset contaminates every later run, so it is retried, then fatal."""
    for attempt in range(5):
        try:
            mechanisms.reset_environment()
            return time.monotonic()
        except Exception as exc:
            print(f"  reset failed ({type(exc).__name__}: {exc}); retrying", flush=True)
            time.sleep(30 * (attempt + 1))
    raise RuntimeError("environment reset failed 5 times; stopping the matrix")


if __name__ == "__main__":
    main()
