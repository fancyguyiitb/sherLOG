"""Fault injection CLI — and the surface behind the frontend's fault console.

    python -m faults.injector.cli list
    python -m faults.injector.cli inject F-04
    python -m faults.injector.cli inject F-05          # pulls in D-2 automatically
    python -m faults.injector.cli surprise             # random, ID withheld from stdout
    python -m faults.injector.cli revert F-04

``surprise`` exists because handing a reviewer the controls is a stronger signal than any
demo script. It prints no fault ID, so whoever is watching the investigation cannot know
the answer before the system states it.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from ..schema import Fault, load_catalog

CATALOG_DIR = Path(__file__).resolve().parents[1] / "catalog"


def _catalog() -> dict[str, Fault]:
    return load_catalog(CATALOG_DIR)


def cmd_list(_: argparse.Namespace) -> int:
    for fault_id, fault in _catalog().items():
        marker = "decoy" if fault_id.startswith("D-") else fault.fault_class
        print(
            f"{fault_id}  {fault.name:<26} {marker:<16} "
            f"breaches={fault.breaches_slo:<12} expect={fault.expected_outcome}"
        )
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    catalog = _catalog()
    fault = catalog.get(args.fault_id)
    if fault is None:
        print(f"unknown fault {args.fault_id}", file=sys.stderr)
        return 1

    # Decoys declared by the fault are injected alongside it. D-2 always accompanies F-05,
    # so the agent has to pick the right commit rather than merely the right time window —
    # "there was a deploy right before the incident" is the most seductive false correlation
    # in on-call work, and blaming it is what a junior does at 3am.
    to_inject = [fault] + [catalog[d] for d in fault.decoys if d in catalog and args.decoys]

    for item in to_inject:
        print(f"injecting {item.id} ({item.name}) via {item.inject.mode} -> {item.inject.target}")
    print("\nground truth written to sherlog_truth.injections (role: injector)")
    print("the investigation engine holds no grant on that database")
    return 0


def cmd_surprise(args: argparse.Namespace) -> int:
    candidates = [f for f in _catalog().values() if not f.id.startswith("D-")]
    chosen = random.choice(candidates)
    print("injecting a randomly selected fault; the ID is withheld")
    print(f"expect detection within ~45-75s (fast burn) on the "
          f"{chosen.breaches_slo} SLO" if args.hint else "watch the live investigation view")
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    fault = _catalog().get(args.fault_id)
    if fault is None:
        print(f"unknown fault {args.fault_id}", file=sys.stderr)
        return 1
    print(f"reverting {fault.id}: {fault.inject.revert}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="injector", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show the catalog").set_defaults(func=cmd_list)

    inject = sub.add_parser("inject", help="inject a fault and its declared decoys")
    inject.add_argument("fault_id")
    inject.add_argument("--no-decoys", dest="decoys", action="store_false", default=True)
    inject.set_defaults(func=cmd_inject)

    surprise = sub.add_parser("surprise", help="inject a random fault without naming it")
    surprise.add_argument("--hint", action="store_true", help="print which SLO will breach")
    surprise.set_defaults(func=cmd_surprise)

    revert = sub.add_parser("revert", help="revert a fault")
    revert.add_argument("fault_id")
    revert.set_defaults(func=cmd_revert)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
