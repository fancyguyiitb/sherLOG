"""The four injection mechanisms. All four are real; none is simulated.

No fault is implemented as ``if FAULT_FLAG: sleep()`` inside application code. That shortcut
would make git correlation and config-diff reasoning fraudulent — the agent would be
"discovering" a change we handed it — and a technical reviewer spots it immediately.

=====  ==============  =======================================================  =============
Code   Mechanism       How                                                      Used by
=====  ==============  =======================================================  =============
M1     config_patch    Rewrite ``runtime/env/<svc>.env``, then force-recreate    F-01, F-02,
                       the container. A real restart and a real config change.   F-03, F-07
M2     git_commit      Apply a code variant to the deploy repo, commit with a    F-05, F-06,
                       plausible author and message, recreate the container,    D-2
                       and write a row to ``deployments``. A real diff.
M3     chaos           Toxiproxy API: a latency toxic on a real network hop.     F-04, D-1,
                                                                                 F-07
M4     data            Insert a crafted catalog row and enqueue a job for it.    F-06
=====  ==============  =======================================================  =============

Reverting is a whole-environment reset rather than a per-fault undo (``reset_environment``):
every toxic removed, every env override cleared, the deploy repo back to its baseline tag,
crafted rows dead-lettered. A fault that cannot be cleanly reverted contaminates every
later run in the eval matrix, and a global reset cannot half-succeed silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg

from ..schema import Fault, Injection
from . import deploy

TOXIPROXY = "http://localhost:8474"
APP_DSN = "postgresql://nimbus:nimbus@localhost:5432/nimbus"
TRUTH_DSN = "postgresql://injector:injector@localhost:5432/sherlog_truth"
APP_SERVICES = ("gateway", "orders-svc", "payments-svc", "psp-stub", "worker")
NULL_SKU = "SKU-NULLPRICE"


@dataclass
class InjectionRecord:
    fault_id: str
    injected_at: datetime
    steps: list[dict[str, Any]] = field(default_factory=list)
    touched: set[str] = field(default_factory=set)


# --- M1 ------------------------------------------------------------------------------

def config_patch(inj: Injection, rec: InjectionRecord) -> None:
    deploy.write_env(inj.target, inj.change)
    deploy.recreate(inj.target)
    rec.touched.add(inj.target)
    rec.steps.append({"mechanism": "config_patch", "service": inj.target, "change": inj.change})


# --- M2 ------------------------------------------------------------------------------

def git_commit(inj: Injection, rec: InjectionRecord, author_kind: str) -> None:
    assert inj.patch_file, "git_commit faults must name a code variant"
    variant = inj.patch_file.rsplit("/", 1)[-1].removesuffix(".py")
    deploy.apply_variant(variant)
    sha, stat = deploy.commit(inj.commit_message or "chore: routine change",
                              deploy.AUTHORS[author_kind])
    deploy.recreate(inj.target)
    author = deploy.AUTHORS[author_kind]
    with psycopg.connect(APP_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO deployments (commit_sha, service, diff_summary, author) "
            "VALUES (%s, %s, %s, %s)",
            (sha, inj.target, f"{inj.commit_message}\n{stat}", author.split(" <")[0]),
        )
    rec.touched.add(inj.target)
    rec.steps.append({"mechanism": "git_commit", "service": inj.target, "commit_sha": sha})


# --- M3 ------------------------------------------------------------------------------

def chaos(inj: Injection, rec: InjectionRecord, fault_id: str) -> None:
    proxy = inj.target.split("/", 1)[-1]
    change = dict(inj.change)
    toxic = str(change.pop("toxic", "latency"))
    body = {
        "name": f"{fault_id}_{toxic}",
        "type": toxic,
        "stream": "downstream",
        "toxicity": 1.0,
        "attributes": {"latency": int(change.get("latency_ms", 0)),
                       "jitter": int(change.get("jitter_ms", 0))},
    }
    r = httpx.post(f"{TOXIPROXY}/proxies/{proxy}/toxics", json=body, timeout=5)
    r.raise_for_status()
    rec.steps.append({"mechanism": "chaos", "proxy": proxy, "toxic": body})


# --- M4 ------------------------------------------------------------------------------

def data(inj: Injection, rec: InjectionRecord) -> None:
    # F-06's code half ships first, as a real deploy; the data trigger arrives after it.
    if inj.patch_file:
        git_commit(inj, rec, "fault")
    with psycopg.connect(APP_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO catalog (sku, name, price_cents, description) "
            "VALUES (%s, 'Gift card (custom value)', NULL, '') ON CONFLICT (sku) DO NOTHING",
            (NULL_SKU,),
        )
        conn.execute("INSERT INTO inventory (sku) VALUES (%s) ON CONFLICT DO NOTHING", (NULL_SKU,))
        order_id = conn.execute(
            "INSERT INTO orders (id, customer_id, status, total_cents) "
            "VALUES (gen_random_uuid(), 'cust-giftcard', 'PENDING', 0) RETURNING id"
        ).fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO jobs (queue, payload) VALUES ('fulfillment', %s)",
            (json.dumps({"order_id": str(order_id), "skus": [NULL_SKU]}),),
        )
    rec.steps.append({"mechanism": "data", "sku": NULL_SKU, "order_id": str(order_id)})


# --- orchestration --------------------------------------------------------------------

def apply(fault: Fault, rec: InjectionRecord, *, as_decoy: bool = False) -> None:
    for inj in [fault.inject, *fault.inject.also]:
        if inj.mode == "config_patch":
            config_patch(inj, rec)
        elif inj.mode == "git_commit":
            git_commit(inj, rec, "decoy" if as_decoy else "fault")
        elif inj.mode == "chaos":
            chaos(inj, rec, fault.id)
        elif inj.mode == "data":
            data(inj, rec)
        else:
            raise ValueError(f"unsupported mode {inj.mode}")


def inject(fault: Fault, decoys: list[Fault]) -> tuple[InjectionRecord, int]:
    """Inject a fault and its decoys; write ground truth where the engine cannot read it."""
    rec = InjectionRecord(fault.id, datetime.now(timezone.utc))
    # Decoys first, so a benign deploy lands *before* the real change — the most seductive
    # ordering, since "the first deploy after things were fine" is what a hurried human blames.
    for d in decoys:
        apply(d, rec, as_decoy=True)
    apply(fault, rec)
    injection_id = record_truth(fault, decoys, rec)
    return rec, injection_id


def record_truth(fault: Fault, decoys: list[Fault], rec: InjectionRecord) -> int:
    with psycopg.connect(TRUTH_DSN, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO injections (fault_id, injected_at, decoys, ground_truth, catalog_version) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (fault.id, rec.injected_at, [d.id for d in decoys],
             json.dumps({**fault.ground_truth.model_dump(), "steps": rec.steps}),
             "v1"),
        ).fetchone()
    return int(row[0])  # type: ignore[index]


def reset_environment() -> None:
    """Back to a clean baseline: no toxics, no overrides, baseline code, no crafted rows."""
    httpx.post(f"{TOXIPROXY}/reset", timeout=5).raise_for_status()

    dirty: set[str] = set()
    if deploy.ENV_DIR.exists():
        for path in deploy.ENV_DIR.glob("*.env"):
            if path.read_text(encoding="utf-8").strip():
                dirty.add(path.stem)
                deploy.clear_env(path.stem)

    if deploy.git("rev-parse", "HEAD").strip() != deploy.git("rev-parse", deploy.BASELINE_TAG).strip():
        changed = deploy.git("diff", "--name-only", deploy.BASELINE_TAG, "HEAD").splitlines()
        deploy.git("reset", "-q", "--hard", deploy.BASELINE_TAG)
        deploy.git("clean", "-qfd")
        for f in changed:
            parts = f.split("/")
            if len(parts) > 2 and parts[0] == "nimbus":
                dirty.add(parts[1].replace("_svc", "-svc").replace("_stub", "-stub"))

    with psycopg.connect(APP_DSN, autocommit=True) as conn:
        conn.execute(
            "UPDATE jobs SET status='DEAD', last_error='environment reset' "
            "WHERE status='PENDING' AND payload->'skus' ? %s", (NULL_SKU,))
        conn.execute("DELETE FROM inventory WHERE sku = %s", (NULL_SKU,))
        conn.execute("DELETE FROM catalog WHERE sku = %s", (NULL_SKU,))
        conn.execute("DELETE FROM deployments")
        conn.execute(
            "INSERT INTO deployments (commit_sha, service, deployed_at, diff_summary, author) "
            "VALUES (%s, 'all', now() - interval '1 day', 'release: nimbus store', 'Nimbus Deploy')",
            (deploy.git("rev-parse", "--short=7", deploy.BASELINE_TAG).strip(),),
        )
        conn.execute(
            "UPDATE orders SET status='COMPLETE', completed_at=now() WHERE customer_id='cust-giftcard'")

    with psycopg.connect(TRUTH_DSN, autocommit=True) as conn:
        conn.execute("UPDATE injections SET reverted_at = now() WHERE reverted_at IS NULL")

    services = sorted(s for s in dirty if s in (*APP_SERVICES, "redis"))
    if services:
        deploy.recreate(*services)
