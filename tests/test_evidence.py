"""Evidence identity, metric shaping, log templating, and the tool contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sherlog.evidence.base import TOOL_NAMES, TOOL_SPECS, ToolRegistry
from sherlog.evidence.series import MAX_POINTS, downsample, summarise
from sherlog.evidence.templates import TemplateStat, delta_pct, is_novel, rank
from sherlog.models import Evidence, evidence_id

T0 = datetime(2026, 8, 15, 14, 30, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)


# --- content addressing ------------------------------------------------------------------

def test_the_same_query_yields_the_same_id():
    """The property everything else leans on.

    Two subagents issuing the same query get the same ID, so citations are stable, the
    retrieval cache is automatic, we never pay twice for one retrieval, and posterior
    deduplication is exact rather than best-effort.
    """
    a = Evidence("metric", "db_pool_waiters", T0, T1, "content A", "prom")
    b = Evidence("metric", "db_pool_waiters", T0, T1, "content B — fetched later", "prom")
    assert a.id == b.id


def test_a_different_window_yields_a_different_id():
    a = Evidence("metric", "db_pool_waiters", T0, T1, "x", "prom")
    b = Evidence("metric", "db_pool_waiters", T0, T1 + timedelta(minutes=1), "x", "prom")
    assert a.id != b.id


def test_ids_are_timezone_normalised():
    """A naive datetime and its UTC equivalent must not produce two IDs for one retrieval."""
    naive = datetime(2026, 8, 15, 14, 30)
    assert evidence_id("log", "q", naive, T1) == evidence_id("log", "q", T0, T1)


def test_ids_are_short_and_citable():
    assert Evidence("log", "q", T0, T1, "c", "s").id.startswith("ev_")
    assert len(Evidence("log", "q", T0, T1, "c", "s").id) == 11


def test_window_containment_backs_the_temporal_check():
    ev = Evidence("log", "q", T0, T1, "c", "s")
    assert ev.covers(T0 + timedelta(minutes=5))
    assert not ev.covers(T0 - timedelta(minutes=1))


# --- the tool contract ---------------------------------------------------------------------

def test_the_tool_contract_is_seven_tools():
    """Seven, not forty-five. These are the entire interface between sherLOG and any
    monitored system; swapping Loki for CloudWatch means reimplementing seven functions
    behind an unchanged signature."""
    assert len(TOOL_SPECS) == 7
    assert set(TOOL_NAMES) == {
        "log_stats", "log_sample", "metric_query", "trace_search",
        "git_diff_since", "runtime_state", "doc_search",
    }


def test_an_undeclared_tool_cannot_be_registered():
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register("kubectl_delete", lambda **kw: None)


def test_a_declared_tool_registers_and_resolves():
    registry = ToolRegistry()
    impl = lambda **kw: Evidence("log", "q", T0, T1, "c", "s")  # noqa: E731
    registry.register("log_stats", impl)
    assert registry.get("log_stats") is impl
    assert registry.implemented == ("log_stats",)


# --- metric shaping ---------------------------------------------------------------------------

def test_downsampling_respects_the_cap():
    points = [(T0 + timedelta(seconds=10 * i), float(i)) for i in range(600)]
    assert len(downsample(points)) == MAX_POINTS


def test_downsampling_preserves_spikes():
    """Max-pooling, not averaging. This data exists to find latency spikes and saturation,
    and averaging is exactly the operation that hides them."""
    points = [(T0 + timedelta(seconds=i), 1.0) for i in range(500)]
    points[321] = (points[321][0], 99.0)
    assert max(v for _, v in downsample(points)) == 99.0


def test_short_series_pass_through_untouched():
    points = [(T0 + timedelta(seconds=i), float(i)) for i in range(10)]
    assert downsample(points) == points


def test_summary_computes_the_baseline_delta():
    """The model sees shape and change, never a raw series. Absolute values are useless
    without a baseline it does not otherwise have."""
    current = [(T0 + timedelta(seconds=i), 2.0) for i in range(100)]
    baseline = [(T0 + timedelta(seconds=i), 1.0) for i in range(100)]
    s = summarise({"service": "orders-svc"}, current, baseline)
    assert s.delta_ratio == pytest.approx(1.0)
    assert "up 100%" in s.describe()


# --- log templating ------------------------------------------------------------------------------

def test_a_template_first_seen_inside_the_window_is_novel():
    """F-06's primary signal: the worker's crash handler emits a line that has never been
    emitted before."""
    assert is_novel(first_seen=T0 + timedelta(minutes=1), window_start=T0)
    assert not is_novel(first_seen=T0 - timedelta(days=3), window_start=T0)


def test_tiny_counts_produce_no_delta():
    """Reporting "+900%" on a template that went from 1 to 10 occurrences trains the agent
    to chase noise."""
    assert delta_pct(count=10, baseline_count=1) is None


def test_a_real_surge_produces_a_delta():
    assert delta_pct(count=4200, baseline_count=1000) == pytest.approx(320.0)


def test_ranking_puts_novelty_above_raw_volume():
    """The highest-count template is almost always a healthy access log. Ranking by count
    would bury the one line that matters under the 4,000 that do not."""
    stats = [
        TemplateStat("T-001", "GET /healthz ok", 40000, 39000, T0, T1, False, 2.5),
        TemplateStat("T-091", "job handler raised <*>", 12, 0, T0, T1, True, None),
    ]
    assert rank(stats)[0].template_id == "T-091"
