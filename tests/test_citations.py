"""Citation verification — the headline guardrail.

Each test corresponds to one way a model fabricates: inventing an ID, inventing a
timestamp, inventing a number, inventing an entity. None of these checks involves a model,
which is the point: using an LLM to check an LLM as the primary defence just moves the
failure one level up.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sherlog.grounding.citations import verify_all, verify_claim
from sherlog.models import Claim, Evidence
from sherlog.redaction import redact

T0 = datetime(2026, 8, 15, 14, 30, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)


@pytest.fixture
def store():
    ev = redact(
        Evidence(
            kind="metric",
            query='db_pool_waiters{service="orders-svc"}',
            t_start=T0,
            t_end=T1,
            content="db_pool_waiters peaked at 14 on orders-svc; db_pool_size is 3",
            source_ref="prometheus",
        )
    )
    return {ev.id: ev}, ev.id


def test_a_well_cited_claim_passes(store):
    s, eid = store
    claim = Claim(
        text="orders-svc pool saturated with 14 waiters",
        evidence_ids=(eid,),
        timestamp=T0 + timedelta(minutes=2),
        numbers=(14,),
        entities=("orders-svc",),
    )
    assert verify_claim(claim, s).ok


def test_a_fabricated_evidence_id_is_caught(store):
    """The most common failure: a model that produces a citation-shaped string.

    Content-addressed IDs make this check sound — an ID is either a hash of a query we
    actually ran, or it is not in the store.
    """
    s, _ = store
    claim = Claim(text="pool saturated", evidence_ids=("ev_00000000",))
    result = verify_claim(claim, s)
    assert not result.ok
    assert result.failures[0].kind == "MISSING_EVIDENCE"


def test_a_timestamp_outside_the_cited_window_is_caught(store):
    s, eid = store
    claim = Claim(
        text="pool saturated at 09:00",
        evidence_ids=(eid,),
        timestamp=T0 - timedelta(hours=5),
    )
    result = verify_claim(claim, s)
    assert not result.ok
    assert any(f.kind == "TEMPORAL_MISMATCH" for f in result.failures)


def test_an_invented_number_is_caught(store):
    """"47 waiters" when the evidence says 14. The number is plausible, specific, and
    entirely made up — which is exactly the kind of detail that makes a false report
    convincing."""
    s, eid = store
    claim = Claim(text="47 waiters queued", evidence_ids=(eid,), numbers=(47,))
    result = verify_claim(claim, s)
    assert not result.ok
    assert any(f.kind == "NUMBER_NOT_FOUND" for f in result.failures)


def test_rounding_within_tolerance_is_accepted(store):
    """A report legitimately rounds. The tolerance is 5% — wide enough for honest prose,
    narrow enough that an invented figure does not slip through."""
    s, eid = store
    claim = Claim(text="roughly 14 waiters", evidence_ids=(eid,), numbers=(14.3,))
    assert verify_claim(claim, s).ok


def test_an_invented_service_name_is_caught(store):
    s, eid = store
    claim = Claim(
        text="inventory-svc pool saturated",
        evidence_ids=(eid,),
        entities=("inventory-svc",),
    )
    result = verify_claim(claim, s)
    assert not result.ok
    assert any(f.kind == "ENTITY_NOT_FOUND" for f in result.failures)


def test_an_uncited_claim_cannot_pass(store):
    s, _ = store
    result = verify_claim(Claim(text="the pool was fine", evidence_ids=()), s)
    assert not result.ok
    assert result.failures[0].kind == "NO_CITATION"


def test_strip_rate_is_a_reportable_number(store):
    """"3.2% of generated claims failed citation verification and were removed" is a
    measurement. "We prompt it not to hallucinate" is a hope."""
    s, eid = store
    claims = [
        Claim(text="ok", evidence_ids=(eid,), entities=("orders-svc",)),
        Claim(text="ok too", evidence_ids=(eid,), numbers=(3,)),
        Claim(text="fabricated", evidence_ids=("ev_ffffffff",)),
        Claim(text="also fabricated", evidence_ids=(eid,), numbers=(999,)),
    ]
    report = verify_all(claims, s)
    assert report.total == 4
    assert len(report.verified) == 2
    assert report.strip_rate == pytest.approx(0.5)
