"""The fault catalog is validated on every push.

A fault with vague ground truth passes human review by accident. A fault with missing
ground truth should not build at all — every accuracy number in the writeup is computed
against these files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faults.schema import Fault, load_catalog

CATALOG_DIR = Path(__file__).resolve().parents[1] / "faults" / "catalog"


@pytest.fixture(scope="module")
def catalog() -> dict[str, Fault]:
    return load_catalog(CATALOG_DIR)


def test_v1_catalog_is_complete(catalog):
    """Six injectable faults, one abstention case, two decoys.

    Fault count buys eval sample size, not system capability, and sample size is worthless
    until the pipeline is correct. Six faults across four reasoning shapes proves every
    mechanism in the design; faults 7-15 are a data-entry exercise afterwards.
    """
    assert set(catalog) == {"F-01", "F-02", "F-03", "F-04", "F-05", "F-06", "F-07", "D-1", "D-2"}


def test_every_fault_has_a_substantive_causal_chain(catalog):
    for fault in catalog.values():
        assert len(fault.ground_truth.causal_chain) >= 2, fault.id


def test_the_confusable_pairs_name_each_other(catalog):
    """known_misdiagnosis turns "wrong answer" into "wrong answer in the specific way we
    predicted", which is far more informative than a binary."""
    assert catalog["F-01"].ground_truth.known_misdiagnosis == "redis_eviction_storm"
    assert catalog["F-02"].ground_truth.known_misdiagnosis == "cache_ttl_collapse"
    assert catalog["F-03"].ground_truth.known_misdiagnosis == "psp_latency_injection"
    assert catalog["F-04"].ground_truth.known_misdiagnosis == "db_pool_exhaustion"


def test_each_slo_is_breached_by_at_least_one_fault(catalog):
    """Three SLIs chosen so each fault breaches a different one.

    If every fault breached availability, the eval would only ever test one detection path
    and the freshness SLO would be decoration.
    """
    breached = {f.breaches_slo for f in catalog.values() if f.id.startswith("F-")}
    assert {"availability", "latency", "freshness"} <= breached


def test_freshness_is_breached_by_exactly_one_fault(catalog):
    """F-06 is the only fault that never touches the request path. It is the proof that the
    system can diagnose an incident producing no errors and no latency at all."""
    freshness = [f.id for f in catalog.values() if f.breaches_slo == "freshness"]
    assert freshness == ["F-06"]


def test_all_four_injection_mechanisms_are_exercised(catalog):
    """Config patch, git commit, chaos, data. If a mechanism has no fault using it, it is
    untested code that will break the first time it matters."""
    modes = {f.inject.mode for f in catalog.values()}
    assert {"config_patch", "git_commit", "chaos", "data"} <= modes


def test_code_regression_faults_ship_as_real_commits(catalog):
    """Faked git history is instantly obvious to a technical reviewer, and it would make
    git_diff_since() correlation a rehearsal rather than a capability."""
    for fault in catalog.values():
        if fault.inject.mode == "git_commit":
            assert fault.inject.patch_file, fault.id
            assert fault.inject.commit_message, fault.id
            # The message must not name the fault. "inject_fault_F05" would let the agent
            # solve F-05 by reading the commit subject, which tests nothing.
            assert fault.id.lower() not in fault.inject.commit_message.lower()


def test_the_abstention_case_expects_abstention(catalog):
    """The only fault where a confident answer is scored as a failure."""
    f07 = catalog["F-07"]
    assert f07.expected_outcome == "INSUFFICIENT_EVIDENCE"
    assert f07.expected_signals.logs == []
    assert f07.expected_signals.traces == []


def test_decoys_are_never_the_answer(catalog):
    for decoy_id in ("D-1", "D-2"):
        decoy = catalog[decoy_id]
        assert decoy.breaches_slo == "none"
        assert decoy.ground_truth.proximate_service == "none"


def test_the_n_plus_one_fault_always_carries_the_benign_deploy(catalog):
    """"There was a deploy right before the incident" is the single most seductive false
    correlation in real SRE work. F-05 forces the agent to pick the right commit, not
    merely the right time window."""
    assert "D-2" in catalog["F-05"].decoys


def test_every_fault_declares_a_discriminating_signal(catalog):
    for fault in catalog.values():
        if fault.id.startswith("D-"):
            continue
        signals = (
            fault.expected_signals.metrics
            + fault.expected_signals.logs
            + fault.expected_signals.traces
        )
        assert signals, f"{fault.id} declares nothing the evidence layer could check"
