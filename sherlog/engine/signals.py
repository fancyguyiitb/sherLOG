"""Mechanical checking of pre-registered predictions — no model in this loop.

During Hypothesize, every hypothesis must register *checkable* predictions: a tool, its
arguments, and a condition from a fixed vocabulary ("this metric rises vs baseline",
"this log event is present", "this service's config hash changed"...). The prediction is
written before any evidence for the hypothesis is gathered.

Gather then runs each prediction's tool and evaluates its condition in plain code:

    present  -> recorded; the evidence is attached for the model to assess
    absent   -> recorded; costs the hypothesis a fixed −2.0 nats in the posterior,
                outside the per-kind cap. The model never sees a chance to argue it back.
    no data  -> NOT recorded as absent. A service emitting nothing has not shown the signal
                is missing; it has shown an evidence gap. Gaps are surfaced in the report
                and are what drive correct abstention on F-07.

That three-way split is the important design choice. Collapsing "no data" into "absent"
would make a telemetry outage refute every hypothesis about the dark service — a
confident wrong answer produced by the guardrail itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..evidence.tools import ToolResult, Window, run_tool

Condition = Literal[
    "rises", "falls", "stable", "above", "below",  # metric_query
    "present", "absent", "count_rises",            # log_stats (event)
    "config_changed", "commit_changed",            # log_stats (identity)
    "restarted",                                   # runtime_state
    "deploy_present",                              # git_diff_since
    "span_dominates",                              # trace_search
]

CheckTool = Literal["metric_query", "log_stats", "trace_search", "runtime_state",
                    "git_diff_since"]

#: Which conditions each tool can evaluate. A prediction outside this table is rejected at
#: registration — an uncheckable prediction is not a prediction.
VALID: dict[str, set[str]] = {
    "metric_query": {"rises", "falls", "stable", "above", "below"},
    "log_stats": {"present", "absent", "count_rises", "config_changed", "commit_changed"},
    "runtime_state": {"restarted"},
    "git_diff_since": {"deploy_present"},
    "trace_search": {"span_dominates"},
}


class PredictedSignal(BaseModel):
    """A falsifiable prediction, in a form code can check. Every field is required so the
    schema works under strict structured outputs; unused fields are empty / 0."""

    description: str = Field(description="What must be observed if the hypothesis holds.")
    tool: CheckTool
    condition: Condition
    service: str = Field(description="Service for log/trace/state/git checks, else ''.")
    promql: str = Field(description="PromQL for metric_query checks, else ''.")
    event: str = Field(description="Log event name for present/absent/count_rises, else ''.")
    span: str = Field(description="Span name for span_dominates (e.g. db.acquire), else ''.")
    negate: bool = Field(description="true to predict the condition does NOT hold, e.g. "
                                     "'no deployment to orders-svc' = git_diff_since "
                                     "deploy_present with negate=true.")
    threshold: float = Field(description="above/below: absolute value; rises/falls: ratio vs "
                                         "baseline (0 = default 1.5x / 0.67x); stable: max "
                                         "ratio either way (0 = default 1.5x); "
                                         "span_dominates: share 0-1 (0 = default 0.4).")


def validate(sig: PredictedSignal) -> str | None:
    """Return why a prediction is uncheckable, or None if it can be checked."""
    if sig.condition not in VALID[sig.tool]:
        return f"{sig.tool} cannot evaluate '{sig.condition}'"
    if sig.tool == "metric_query" and not sig.promql.strip():
        return "metric_query needs promql"
    if sig.tool in ("log_stats", "trace_search", "runtime_state") and not sig.service:
        return f"{sig.tool} needs a service"
    if sig.condition in ("present", "absent", "count_rises") and not sig.event:
        return f"{sig.condition} needs an event name"
    if sig.condition == "span_dominates" and not sig.span:
        return "span_dominates needs a span name"
    return None


def tool_call(sig: PredictedSignal) -> tuple[str, dict[str, Any]]:
    if sig.tool == "metric_query":
        return "metric_query", {"promql": sig.promql}
    if sig.tool == "log_stats":
        return "log_stats", {"service": sig.service}
    if sig.tool == "trace_search":
        return "trace_search", {"service": sig.service, "min_duration_ms": 300}
    if sig.tool == "runtime_state":
        return "runtime_state", {"service": sig.service}
    return "git_diff_since", {"service": sig.service or None}


@dataclass(frozen=True)
class CheckOutcome:
    present: bool | None          # None = no data: an evidence gap, not an absence
    observed: str                 # one line of what code saw, for the UI and the report


def _finite(x: float) -> bool:
    return x is not None and not (math.isnan(x) or math.isinf(x))


def _metric(sig: PredictedSignal, series: list[dict[str, Any]]) -> CheckOutcome:
    c = sig.condition
    if c == "above":
        peak = max(s["max"] for s in series)
        return CheckOutcome(peak > sig.threshold, f"window max {peak:.4g} vs > {sig.threshold:g}")
    if c == "below":
        low = min(s["mean"] for s in series)
        return CheckOutcome(low < sig.threshold, f"window mean {low:.4g} vs < {sig.threshold:g}")

    # Faults are often step changes (a pool resized, a TTL zeroed), which a window mean
    # dilutes with the pre-change minutes. So both the window mean and the last value are
    # compared against the baseline, taking whichever moved further in the predicted
    # direction.
    if c == "rises":
        thr = sig.threshold if sig.threshold > 1 else 1.5
        hits = []
        for s in series:
            b, m = s["baseline_mean"], max(s["mean"], s["last"])
            if _finite(b) and b > 0:
                hits.append((m / b >= thr, f"{m:.4g} vs baseline {b:.4g} = {m / b:.2f}x"))
            elif _finite(m) and m > 0:
                hits.append((True, f"{m:.4g} from baseline 0"))
            else:
                hits.append((False, f"{m:.4g} (baseline {b})"))
        ok = any(h for h, _ in hits)
        best = next((d for h, d in hits if h), hits[0][1])
        return CheckOutcome(ok, f"rises >= {thr:g}x? {best}")

    if c == "stable":
        # Predicting that something does NOT move: every series stays within [1/k, k] of
        # its baseline (k = threshold, default 1.5), on both mean and last value. A series
        # rising from a zero baseline is not stable.
        k = sig.threshold if sig.threshold > 1 else 1.5
        moved = []
        for s in series:
            b = s["baseline_mean"]
            for v in (s["mean"], s["last"]):
                if _finite(b) and b > 0:
                    if not (1 / k <= v / b <= k):
                        moved.append(f"{v:.4g} vs baseline {b:.4g}")
                elif _finite(v) and v > 0:
                    moved.append(f"{v:.4g} from baseline 0")
        return CheckOutcome(not moved, f"stable within {k:g}x? "
                            + (f"no: {moved[0]}" if moved else "yes"))

    # falls
    thr = sig.threshold if 0 < sig.threshold < 1 else 0.67
    hits = []
    for s in series:
        b, m = s["baseline_mean"], min(s["mean"], s["last"])
        if _finite(b) and b > 0:
            hits.append((m / b <= thr, f"{m:.4g} vs baseline {b:.4g} = {m / b:.2f}x"))
    if not hits:
        return CheckOutcome(None, "no baseline to compare against")
    ok = any(h for h, _ in hits)
    best = next((d for h, d in hits if h), hits[0][1])
    return CheckOutcome(ok, f"falls <= {thr:g}x? {best}")


def evaluate(sig: PredictedSignal, result: ToolResult) -> CheckOutcome:
    """Evaluate a prediction. ``negate`` inverts present/absent; no data stays no data —
    an evidence gap says nothing either way."""
    out = _evaluate(sig, result)
    if sig.negate and out.present is not None:
        return CheckOutcome(not out.present, f"NOT expected: {out.observed}")
    return out


def _evaluate(sig: PredictedSignal, result: ToolResult) -> CheckOutcome:
    f = result.facts
    if result.no_data:
        return CheckOutcome(None, "no data returned — evidence gap")

    if sig.tool == "metric_query":
        series = f.get("series", [])
        if not series:
            return CheckOutcome(None, "no series")
        return _metric(sig, series)

    if sig.tool == "log_stats":
        if sig.condition == "config_changed":
            return CheckOutcome(bool(f.get("config_changed")),
                                f"config_hash changed vs baseline: {f.get('config_changed')}")
        if sig.condition == "commit_changed":
            return CheckOutcome(bool(f.get("commit_changed")),
                                f"commit_sha changed vs baseline: {f.get('commit_changed')}")
        ev = f.get("events", {}).get(sig.event, {"count": 0, "baseline": 0})
        n, b = ev["count"], ev["baseline"]
        if sig.condition == "present":
            need = max(1, int(sig.threshold))
            return CheckOutcome(n >= need, f"{sig.event}: {n} in window (baseline {b})")
        if sig.condition == "absent":
            return CheckOutcome(n == 0, f"{sig.event}: {n} in window (baseline {b})")
        if sig.condition == "count_rises":
            ok = n >= max(2 * b, b + 10)
            return CheckOutcome(ok, f"{sig.event}: {n} vs baseline {b}")

    if sig.tool == "runtime_state":
        r = bool(f.get("restarted_in_window"))
        return CheckOutcome(r, f"{sig.service} (re)started at {f.get('started_at')}: in period={r}")

    if sig.tool == "git_diff_since":
        deploys = f.get("deploys", [])
        if sig.service:
            deploys = [d for d in deploys if d["service"] == sig.service]
        return CheckOutcome(bool(deploys), f"{len(deploys)} deployment(s)"
                            + (f" to {sig.service}" if sig.service else ""))

    if sig.tool == "trace_search":
        thr = sig.threshold if 0 < sig.threshold < 1 else 0.4
        shares = f.get("shares", {})
        match = {k: v for k, v in shares.items() if k.endswith(":" + sig.span) or k.endswith(sig.span)}
        if sig.service:
            match = {k: v for k, v in match.items() if k.startswith(sig.service + ":")} or match
        best = max(match.values(), default=0.0)
        return CheckOutcome(best >= thr, f"span {sig.span} share of slow traces: {best:.0%} "
                                         f"(threshold {thr:.0%})")

    return CheckOutcome(None, "unsupported check")


def check(sig: PredictedSignal, w: Window) -> tuple[ToolResult, CheckOutcome]:
    name, args = tool_call(sig)
    result = run_tool(name, args, w)
    return result, evaluate(sig, result)
