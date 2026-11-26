"""Schema for the fault catalog.

Faults are declarative, versioned and self-labelling. Each YAML carries both the recipe for
injecting the fault and the ground truth about what it does — which is what makes accuracy
a measurement rather than an impression.

The schema is validated in CI. A fault whose ground truth is vague passes review by
accident; a fault whose ground truth is missing a required field does not build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

FaultClass = Literal["resource", "code_regression", "config", "dependency", "data", "observability"]
InjectMode = Literal["config_patch", "git_commit", "chaos", "data", "traffic"]
ExpectedOutcome = Literal["CONFIRMED", "LIKELY", "INSUFFICIENT_EVIDENCE"]


class Injection(BaseModel):
    """How the fault is applied. Every mechanism is real.

    No fault is implemented as ``if FAULT_FLAG: sleep()`` inside application code. That
    shortcut would make git correlation and config-diff reasoning fraudulent — the agent
    would be "finding" a commit we told it about — and a technical reviewer spots it
    immediately.
    """

    mode: InjectMode
    target: str
    change: dict[str, object] = Field(default_factory=dict)
    revert: dict[str, object] = Field(default_factory=dict)
    patch_file: str | None = None  # code variant under faults/variants/ (git_commit, data)
    commit_message: str | None = None
    also: list["Injection"] = Field(default_factory=list)
    """Further injections applied as part of the same fault (F-07: config + toxic)."""


Injection.model_rebuild()


class GroundTruth(BaseModel):
    """The label the investigation engine is never permitted to read.

    Lives in Postgres database ``sherlog_truth``, owned by role ``injector``. The engine
    connects as role ``agent``, which holds no grant on that database at all, and a CI test
    asserts ``agent`` receives a permission error when it tries. Convention is not enough:
    if a reviewer suspects leakage, every accuracy number in the writeup is void.
    """

    root_cause: str
    proximate_service: str  # first service on the causal path whose own behaviour degrades
    originating_component: str  # where it starts
    causal_chain: list[str] = Field(min_length=2)
    correct_fix_class: str
    known_misdiagnosis: str | None = None
    """The specific wrong answer we predict. Turns "wrong" into "wrong in the way we
    predicted", which is a far more informative metric than a binary."""

    @field_validator("root_cause")
    @classmethod
    def _substantive(cls, v: str) -> str:
        if len(v.split()) < 6:
            raise ValueError(
                "root_cause must be a real sentence; a two-word label cannot be scored "
                "for semantic equivalence"
            )
        return v


class ExpectedSignals(BaseModel):
    metrics: list[str] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    traces: list[str] = Field(default_factory=list)


class Fault(BaseModel):
    id: str
    name: str
    fault_class: FaultClass = Field(alias="class")
    severity: Literal["low", "medium", "high"]
    inject: Injection
    ground_truth: GroundTruth
    expected_signals: ExpectedSignals = Field(default_factory=ExpectedSignals)
    breaches_slo: Literal["availability", "latency", "freshness", "none"]
    decoys: list[str] = Field(default_factory=list)
    expected_outcome: ExpectedOutcome = "CONFIRMED"
    notes: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not (v.startswith(("F-", "D-")) and v[2:].isdigit()):
            raise ValueError("fault ids look like F-01; decoy ids look like D-1")
        return v


def load_fault(path: Path) -> Fault:
    return Fault.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_catalog(directory: Path) -> dict[str, Fault]:
    """Load and validate every fault in the catalog, keyed by ID."""
    catalog: dict[str, Fault] = {}
    for path in sorted(directory.glob("*.yaml")):
        fault = load_fault(path)
        if fault.id in catalog:
            raise ValueError(f"duplicate fault id {fault.id} at {path}")
        catalog[fault.id] = fault
    return catalog
