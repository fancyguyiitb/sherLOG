"""The mechanical prediction checker, the claim extractor, the gate, the deploy-repo leak
guard and the scorer — every deterministic piece added for the live engine."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from eval.score import score
from faults.injector.deploy import build_source
from sherlog.engine.outcome import decide
from sherlog.engine.run import to_claim
from sherlog.engine.signals import PredictedSignal, evaluate, validate
from sherlog.evidence.tools import ToolResult
from sherlog.models import Evidence, Hypothesis

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def sig(**kw) -> PredictedSignal:
    base = dict(description="d", tool="metric_query", condition="rises", service="",
                promql="x", event="", span="", threshold=0.0, negate=False)
    return PredictedSignal(**{**base, **kw})


def result(facts, no_data=False) -> ToolResult:
    ev = Evidence("metric", "q", T0, T0 + timedelta(minutes=4), "c", "s")
    return ToolResult(ev, facts, no_data=no_data)


def series(mean, base, mx=None):
    return {"series": [{"labels": {}, "mean": mean, "max": mx if mx is not None else mean,
                        "last": mean, "baseline_mean": base, "ratio": mean / base if base else math.nan}]}


def test_rises_present_and_absent():
    assert evaluate(sig(), result(series(10, 2))).present is True
    assert evaluate(sig(), result(series(2.1, 2))).present is False


def test_rise_from_zero_counts_as_rise():
    assert evaluate(sig(), result(series(5, 0))).present is True


def test_no_data_is_a_gap_not_an_absence():
    """The design's key split: a dark service must not refute hypotheses about itself."""
    out = evaluate(sig(), result({"series": []}, no_data=True))
    assert out.present is None


def test_log_event_presence_and_identity_change():
    facts = {"events": {"db.acquire.timeout": {"count": 40, "baseline": 0}},
             "config_changed": True, "commit_changed": False}
    s = sig(tool="log_stats", condition="present", service="orders-svc", event="db.acquire.timeout")
    assert evaluate(s, result(facts)).present is True
    s2 = sig(tool="log_stats", condition="absent", service="orders-svc", event="cache.error")
    assert evaluate(s2, result(facts)).present is True
    s3 = sig(tool="log_stats", condition="commit_changed", service="orders-svc")
    assert evaluate(s3, result(facts)).present is False


def test_uncheckable_predictions_are_rejected():
    assert validate(sig(tool="runtime_state", condition="rises", service="redis"))
    assert validate(sig(tool="metric_query", condition="rises", promql=""))
    assert validate(sig(tool="log_stats", condition="present", service="worker", event=""))
    assert validate(sig()) is None


def hyp(i, p):
    h = Hypothesis(i, "s", ["x"])
    h.posterior = p
    return h


def test_stripped_claims_never_turn_an_abstention_into_a_conclusion():
    d = decide([hyp("H1", 0.40), hyp("H2", 0.35), hyp("H3", 0.25)], critic_concurs=True,
               unverified_claims=2, verified_claims=1)
    assert d.outcome == "INSUFFICIENT_EVIDENCE"


def test_report_with_no_surviving_claim_abstains():
    d = decide([hyp("H1", 0.95), hyp("H2", 0.03), hyp("H3", 0.02)], critic_concurs=True,
               unverified_claims=3, verified_claims=0)
    assert d.outcome == "INSUFFICIENT_EVIDENCE"
    assert d.reason == "no_verified_claims"


def test_claim_numbers_and_entities_are_extracted_by_code_not_declared():
    c = to_claim("At 08:12:03Z orders-svc pool size dropped to 1 after deploy a3f9c21 (H2, ev_12345678)",
                 ["ev_12345678"], "")
    assert c.numbers == (1.0,)            # times, hypothesis and evidence ids excluded
    assert set(c.entities) == {"orders-svc", "a3f9c21"}


def test_deploy_repo_build_strips_every_comment_and_docstring():
    src = '"""Module doc naming F-06."""\nX = 1  # F-06 deletes this\n\ndef f():\n    """F-05 lives here."""\n    return X\n'
    built = build_source(src)
    assert "F-0" not in built and "#" not in built
    assert "def f():" in built and "return X" in built


def test_score_f04_trap_scores_service_but_not_origin():
    run = {
        "id": "inv_x", "incident": {"detected_at": (T0 + timedelta(seconds=70)).isoformat()},
        "ended_at": (T0 + timedelta(seconds=200)).isoformat(),
        "decision": {"outcome": "LIKELY", "confidence": 0.7},
        "report": {"proximate_service": "payments-svc", "originating_component": "orders-svc",
                   "root_cause": "orders-svc pool exhausted"},
        "ledger": {"hypotheses": [{"id": "H1", "statement": "orders-svc DB pool too small"}],
                   "budget": {}},
        "hypotheses_meta": {"H1": {"component": "orders-svc", "fault_class": "resource"}},
        "verification": {"total": 4, "verified": 4, "stripped": 0}, "stages": [],
    }
    inj = {"fault_id": "F-04", "injected_at": T0.isoformat(), "decoys": [],
           "ground_truth": {"proximate_service": "payments-svc", "originating_component": "psp-stub"}}
    s = score(run, inj)
    assert s["proximate_correct"] and not s["originating_correct"]
    assert s["rca"] == 0.0 and s["mttd_s"] == pytest.approx(70)


def test_planted_canary_never_survives_to_a_rendered_prompt():
    """The canary is planted in payments-svc's startup log and a runbook. Whatever path it
    takes into evidence, the rendered model payload must be clean."""
    from sherlog.llm.client import render_evidence
    from sherlog.redaction import CANARY_SECRET, canary_clean, redact

    line = '{"event": "config.loaded", "attrs": {"psp_api_key": "%s"}}' % CANARY_SECRET
    ev = Evidence("log", "log_sample(...)", T0, T0 + timedelta(minutes=5), line, "loki")
    assert canary_clean(render_evidence([redact(ev)]))


def test_stable_condition_for_predicting_no_change():
    s = sig(condition="stable")
    assert evaluate(s, result(series(2.1, 2))).present is True
    assert evaluate(s, result(series(9, 2))).present is False
    assert evaluate(s, result(series(5, 0))).present is False


def test_negated_prediction_inverts_but_never_invents_data():
    s = sig(tool="git_diff_since", condition="deploy_present", service="orders-svc", negate=True)
    assert evaluate(s, result({"deploys": []})).present is True        # predicted: no deploy
    assert evaluate(s, result({"deploys": [{"service": "orders-svc", "sha": "a", "at": "t"}]})).present is False
    gap = sig(condition="rises", negate=True)
    assert evaluate(gap, result({"series": []}, no_data=True)).present is None


def test_duplicate_hypotheses_merge_but_distinct_mechanisms_do_not():
    from types import SimpleNamespace as NS

    from sherlog.engine.run import dedupe_hypotheses
    evict = sig(promql="rate(redis_evicted_keys_total[1m])")
    miss = sig(promql="discriminator:cache_miss_rate")
    restart = sig(tool="runtime_state", condition="restarted", service="redis", promql="")
    pool = sig(promql="db_pool_size", condition="falls")
    h1 = NS(id="H1", component="redis", fault_class="config")
    h4 = NS(id="H4", component="redis", fault_class="config")
    ttl = NS(id="H2", component="orders-svc", fault_class="config")
    poolh = NS(id="H3", component="orders-svc", fault_class="config")
    out = dedupe_hypotheses([(h1, [evict, miss, restart]), (h4, [evict, miss]),
                             (ttl, [miss, restart]), (poolh, [pool])])
    assert [h.id for h, _ in out] == ["H1", "H2", "H3"]


def test_coverage_gaps_flag_a_service_that_went_dark():
    from sherlog.engine.run import coverage_gaps

    vol = result({"volumes": {
        "payments-svc": {"count": 7000, "baseline": 12000, "last_minute": 3,
                         "baseline_per_minute": 2700.0},
        "orders-svc": {"count": 20000, "baseline": 18000, "last_minute": 4800,
                       "baseline_per_minute": 4000.0}}})
    gaps = coverage_gaps("log_stats", {"service": "*"}, vol)
    assert len(gaps) == 1 and gaps[0].startswith("payments-svc")
    up = result({"series": [
        {"labels": {"instance": "payments-svc"}, "baseline_mean": 1.0, "last": 0.0},
        {"labels": {"instance": "worker"}, "baseline_mean": 0.0, "last": 0.0}]})
    gaps = coverage_gaps("metric_query", {"promql": "up"}, up)
    assert len(gaps) == 1 and gaps[0].startswith("payments-svc")


def test_no_conclusion_about_a_component_that_went_dark():
    d = decide([hyp("H1", 0.90), hyp("H2", 0.05), hyp("H3", 0.05)], critic_concurs=True,
               unverified_claims=0, verified_claims=4, leader_unobservable=True)
    assert d.outcome == "INSUFFICIENT_EVIDENCE" and d.reason == "evidence_gap"


def test_a_quieter_service_is_not_a_dark_one():
    """F-06's worker, stuck retrying one job, logs ~10% of its usual volume. That is the
    fault's symptom, not missing telemetry, and must not block the diagnosis."""
    from sherlog.engine.run import coverage_gaps

    vol = result({"volumes": {"worker": {"count": 900, "baseline": 9000, "last_minute": 180,
                                         "baseline_per_minute": 1885.0}}})
    assert coverage_gaps("log_stats", {"service": "*"}, vol) == []
