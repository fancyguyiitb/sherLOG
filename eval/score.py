"""Score one investigation against the ground truth the injector wrote.

Every metric here is computed by code. The v1 build has no LLM judge (see the dated
revision in docs/SCORING.md): metric 1 is scored mechanically from the leading hypothesis's
originating component and fault class against the table below, which was written before
any scored run existed. Causal-chain F1 (metric 4) is deferred with the judge.

This module runs in the harness, outside the engine. It reads ``sherlog_truth`` as role
``injector``; the engine itself holds no grant on that database.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

#: Accepted (originating component, fault classes) per fault for RCA accuracy@1.
#: F-02: a Redis maxmemory change is fairly described as a dependency, config or resource
#: fault — what separates it from F-01 is the component (redis vs orders-svc).
#: F-03: a pool shrunk by config is fairly called config or resource.
#: F-06: code_regression (the missing dead-letter branch) is full credit; naming only the
#: bad row ("data") is the trigger, not the defect: half credit, per SCORING.md §4.
#: ``mechanism``: the leading hypothesis's statement must also name the mechanism. F-01 and
#: F-03 share a component AND a fault class (orders-svc, config), so without this a
#: cache-TTL diagnosis would score as correct on a pool fault. Matching is a case-insensitive
#: regex over the statement — deliberately permissive on wording, strict on substance.
ACCEPT: dict[str, dict[str, Any]] = {
    "F-01": {"component": "orders-svc", "full": {"config"}, "half": set(),
             "mechanism": r"ttl|cache.{0,40}(disabl|zero|expir|retain|retention|never|bypass|config)|CATALOG_CACHE"},
    "F-02": {"component": "redis", "full": {"dependency", "config", "resource"}, "half": set(),
             "mechanism": r"evict|maxmemory|memory (limit|pressure|cap)|out of memory"},
    "F-03": {"component": "orders-svc", "full": {"config", "resource"}, "half": set(),
             "mechanism": r"pool.{0,40}(size|max|reduc|shr[iu]nk|small|limit|config|1\b|one)|DB_POOL_MAX"},
    "F-04": {"component": "psp-stub", "full": {"dependency"}, "half": set(),
             "mechanism": r"psp|payment provider|provider|external payment"},
    "F-05": {"component": "orders-svc", "full": {"code_regression"}, "half": set(),
             "mechanism": r"n\+1|per.item|per.sku|loop|one query per|enrich|batch"},
    "F-06": {"component": "worker", "full": {"code_regression"}, "half": {"data"},
             "mechanism": r"dead.?letter|retr(y|ies)|null.{0,20}price|head.of.line|stuck job|poison"},
}

_D1_RE = re.compile(r"latenc|slow|proxy|network", re.I)
_D1_NOT = re.compile(r"evict|maxmemory|memory", re.I)
_D2_RE = re.compile(r"log (message|string|wording)|readme|clarify", re.I)


def _leader(run: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    hyps = run.get("ledger", {}).get("hypotheses", [])
    if not hyps:
        return None, {}
    top = hyps[0]
    return top, run.get("hypotheses_meta", {}).get(top["id"], {})


def decoy_blamed(run: dict[str, Any], truth: dict[str, Any], decoys: list[str]) -> bool:
    top, meta = _leader(run)
    if top is None or run.get("decision", {}).get("outcome") == "INSUFFICIENT_EVIDENCE":
        return False
    text = top["statement"] + " " + run.get("report", {}).get("root_cause", "")
    for d in decoys:
        if d == "D-1" and meta.get("component") == "redis" and _D1_RE.search(text) \
                and not _D1_NOT.search(text):
            return True
        if d == "D-2":
            shas = [s.get("commit_sha") for s in truth.get("steps", [])
                    if s.get("mechanism") == "git_commit"]
            # D-2 only accompanies F-05 / F-06, which also commit; decoys are applied
            # first, so the decoy's commit is the first git step of two.
            decoy_sha = shas[0] if len(shas) >= 2 else None
            if (decoy_sha and decoy_sha in text) or _D2_RE.search(text):
                return True
    return False


def score(run: dict[str, Any], injection: dict[str, Any]) -> dict[str, Any]:
    fault = injection["fault_id"]
    truth = {**injection["ground_truth"], "_fault": fault}
    decoys = list(injection.get("decoys") or [])
    decision = run.get("decision") or {}
    outcome = decision.get("outcome", "INSUFFICIENT_EVIDENCE")
    report = run.get("report") or {}
    top, meta = _leader(run)
    abstained = outcome == "INSUFFICIENT_EVIDENCE"

    proximate_ok = report.get("proximate_service") == truth["proximate_service"]
    origin_ok = report.get("originating_component") == truth["originating_component"]

    if fault == "F-07":
        names_gap = bool(run.get("evidence_gaps")) or bool(report.get("evidence_gaps"))
        rca = 1.0 if (abstained and proximate_ok and names_gap) else 0.0
        abstention_ok = abstained
    else:
        acc = ACCEPT[fault]
        cls = meta.get("fault_class")
        rca = 0.0
        names_mechanism = bool(re.search(acc["mechanism"], top["statement"] if top else "", re.I))
        if not abstained and meta.get("component") == acc["component"] and names_mechanism:
            rca = 1.0 if cls in acc["full"] else 0.5 if cls in acc["half"] else 0.0
        abstention_ok = not abstained

    blamed = decoy_blamed(run, truth, decoys)
    if blamed:
        rca = 0.0

    v = run.get("verification") or {}
    total = v.get("total", 0)
    detected = datetime.fromisoformat(run["incident"]["detected_at"])
    injected = datetime.fromisoformat(str(injection["injected_at"]))
    ended = datetime.fromisoformat(run["ended_at"]) if run.get("ended_at") else None
    cost = sum(s.get("cost_usd", 0) for s in run.get("stages", []))
    return {
        "investigation_id": run["id"], "fault_id": fault, "decoys": decoys,
        "outcome": outcome, "confidence": decision.get("confidence"),
        "rca": rca, "proximate_correct": proximate_ok, "originating_correct": origin_ok,
        "decoy_blamed": blamed, "abstained": abstained, "abstention_correct": abstention_ok,
        "claims_total": total, "claims_stripped": v.get("stripped", 0),
        "evidence_precision": (v.get("verified", 0) / total) if total else None,
        "hallucination_rate": (v.get("stripped", 0) / total) if total else None,
        "mttd_s": (detected - injected).total_seconds(),
        "mttr_diagnosis_s": (ended - detected).total_seconds() if ended else None,
        "cost_usd": round(cost, 4),
        "tokens": run.get("ledger", {}).get("budget", {}).get("tokens"),
        "tool_calls": run.get("ledger", {}).get("budget", {}).get("tool_calls"),
        "degraded": run.get("degraded", False), "error": run.get("error"),
        "leader": top["statement"] if top else None,
        "leader_component": meta.get("component"), "leader_class": meta.get("fault_class"),
    }
