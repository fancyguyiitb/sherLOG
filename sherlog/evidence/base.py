"""The evidence layer's fixed interface.

These seven tools are the *entire* surface between sherLOG and any monitored system.
Swapping Loki for CloudWatch or Prometheus for Datadog means reimplementing seven
functions behind an unchanged signature — the investigation engine is not touched. That
property is the whole reason the interface is declared in one place and kept this small.

Every tool is deterministic and holds no model. Every tool returns an ``Evidence`` object,
which reaches a prompt only through the redaction gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from ..models import Evidence, EvidenceKind


class EvidenceTool(Protocol):
    name: str
    kind: EvidenceKind

    def __call__(self, **kwargs: object) -> Evidence: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What the model is told a tool does, and what the registry enforces about it."""

    name: str
    kind: EvidenceKind
    description: str
    backend: str


#: The contract between Plane 2 (observability) and Plane 4 (evidence). This table is the
#: part that must not be allowed to drift, so it lives in code and is asserted in tests.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "log_stats",
        "log",
        "Aggregate log statistics over a window, grouped by Drain3 template: per-template "
        "count, delta against the same window 24h prior, and a novelty flag for templates "
        "absent from the prior 7 days. Never returns raw lines. Always called before "
        "sampling — statistics first is what keeps log volume from eating the context.",
        "loki",
    ),
    ToolSpec(
        "log_sample",
        "log",
        "At most 50 raw lines matching one template ID. The cap is enforced in code, not "
        "requested in the prompt.",
        "loki",
    ),
    ToolSpec(
        "metric_query",
        "metric",
        "Execute PromQL, downsample to <=120 points, and attach the same query shifted 24h "
        "as a baseline plus a computed delta. The model sees shape and change, never a raw "
        "series.",
        "prometheus",
    ),
    ToolSpec(
        "trace_search",
        "trace",
        "TraceQL for slow exemplars, returning a span-duration breakdown rather than full "
        "trace JSON. Also resolves a trace_id lifted from a log line.",
        "tempo",
    ),
    ToolSpec(
        "git_diff_since",
        "git",
        "Deployments in the window from the `deployments` table, joined to `git show` "
        "output and truncated per file.",
        "postgres+git",
    ),
    ToolSpec(
        "runtime_state",
        "state",
        "Restart count, OOM kills, current CPU/memory, and the current config hash with "
        "the time it last changed. The config-hash delta is what separates a config fault "
        "from a load-induced one.",
        "cadvisor+docker",
    ),
    ToolSpec(
        "doc_search",
        "doc",
        "Hybrid retrieval (BM25 + dense embeddings, reciprocal-rank fusion) over the "
        "private corpus, gated by classification tier.",
        "postgres+pgvector",
    ),
)

TOOL_NAMES = tuple(spec.name for spec in TOOL_SPECS)


class ToolRegistry:
    """Binds tool names to implementations and charges the budget governor per call.

    Registration is checked against ``TOOL_SPECS``: a tool the contract does not declare
    cannot be registered, which is how the Plane 2 / Plane 4 boundary stays fixed.
    """

    def __init__(self) -> None:
        self._impls: dict[str, Callable[..., Evidence]] = {}

    def register(self, name: str, impl: Callable[..., Evidence]) -> None:
        if name not in TOOL_NAMES:
            raise ValueError(f"{name!r} is not in the declared tool contract: {TOOL_NAMES}")
        self._impls[name] = impl

    def get(self, name: str) -> Callable[..., Evidence]:
        if name not in self._impls:
            raise KeyError(f"tool {name!r} is declared but not implemented")
        return self._impls[name]

    @property
    def implemented(self) -> tuple[str, ...]:
        return tuple(sorted(self._impls))


def window(t_start: datetime, t_end: datetime) -> tuple[datetime, datetime]:
    if t_end <= t_start:
        raise ValueError("evidence window must be non-empty and forward in time")
    return t_start, t_end
