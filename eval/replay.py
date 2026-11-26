"""Re-run an investigation against a past incident's windows. Telemetry history is still in
Loki / Prometheus / Tempo, so this is the same evidence the original run could see — a
fast, injection-free loop for engine development. Re-scores against the original truth.

Caveat: runtime_state reads the container as it is NOW (Docker has no history), so after the
environment reset it no longer shows the faulted config. Replays are faithful for metrics,
logs, traces and deployments only; never report replay numbers as eval results."""

from __future__ import annotations

import json
import sys

from eval.run import fetch_injection
from eval.score import score
from sherlog.config import SETTINGS
from sherlog.engine.run import investigate
from sherlog.llm.client import OpenAIClient


def main() -> None:
    results_file, key = sys.argv[1], sys.argv[2]
    row = next(json.loads(l) for l in open(results_file, encoding="utf-8")
               if json.loads(l)["key"] == key)
    orig = json.loads((SETTINGS.investigations_dir / f"{row['investigation_id']}.json").read_text())
    run = investigate(orig["incident"], OpenAIClient(), label=f"replay:{key}")
    s = score(run.to_dict(), fetch_injection(row["injection_id"]))
    print(json.dumps({k: s[k] for k in ("outcome", "confidence", "rca", "proximate_correct",
                                        "originating_correct", "cost_usd", "leader")}, indent=1))
    print("investigation:", run.id)


if __name__ == "__main__":
    main()
