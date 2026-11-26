"""Structured-output schemas, one per stage. Every model call returns one of these — no
free-text parsing anywhere. Fields are all required (strict structured outputs); "unused"
is expressed as an empty string or list.

Note what is absent: no schema has a confidence or probability field. Models label; code
computes (``sherlog.engine.posterior``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..models import Verdict
from .signals import PredictedSignal

Component = Literal["gateway", "orders-svc", "payments-svc", "psp-stub", "worker", "redis",
                    "postgres", "unknown"]


class TriageOut(BaseModel):
    summary: str = Field(description="Two sentences: what is breaching and where it is visible.")
    symptom: Literal["errors", "latency", "staleness", "mixed"]
    suspect_services: list[Component]
    notable: list[str] = Field(description="Up to 5 observations from the snapshot worth "
                                           "explaining, each citing an evidence id in brackets.")


class HypothesisOut(BaseModel):
    id: str = Field(description="H1, H2, ...")
    statement: str = Field(description="One sentence naming the root cause and its mechanism.")
    component: Component = Field(description="Where the fault ORIGINATES (not where it hurts).")
    fault_class: Literal["config", "code_regression", "dependency", "resource", "data",
                         "observability", "unknown"]
    predicted_signals: list[PredictedSignal] = Field(
        description="2-4 checkable predictions. At least one must DISCRIMINATE: it should be "
                    "false under the competing hypotheses.")


class HypothesizeOut(BaseModel):
    hypotheses: list[HypothesisOut]


class ToolRequest(BaseModel):
    tool: Literal["metric_query", "log_stats", "log_sample", "trace_search", "git_diff_since",
                  "runtime_state", "doc_search"]
    service: str
    promql: str
    event: str
    query: str = Field(description="doc_search query text, else ''.")
    min_duration_ms: int
    why: str


class GatherOut(BaseModel):
    requests: list[ToolRequest] = Field(description="0-3 additional queries.")


class AssessmentOut(BaseModel):
    hypothesis_id: str
    evidence_id: str
    verdict: Verdict
    rationale: str = Field(description="One sentence, shown in the UI.")


class AssessOut(BaseModel):
    assessments: list[AssessmentOut]


class CriticOut(BaseModel):
    verdict: Literal["CONCUR", "DISSENT"]
    strongest_objection: str
    better_explanation: str = Field(description="A competing explanation the evidence fits "
                                                "better, or '' if none.")
    missing_evidence: list[str]
    alternative_predictions: list[PredictedSignal] = Field(
        description="If DISSENT: 1-3 checkable predictions of the better explanation. "
                    "Empty if CONCUR.")


class ClaimOut(BaseModel):
    text: str = Field(description="One factual sentence. Every number and name in it must "
                                  "appear in the cited evidence.")
    evidence_ids: list[str]
    timestamp: str = Field(description="ISO-8601 UTC instant the claim refers to, or ''.")


class ReportOut(BaseModel):
    root_cause: str
    proximate_service: Component = Field(
        description="The first service on the causal path whose OWN behaviour degrades "
                    "(its latency, errors or throughput change), i.e. where the fault first "
                    "becomes visible in a service we run. Not the gateway merely because "
                    "users see errors there; the gateway is proximate only if the gateway "
                    "itself is what degraded first.")
    originating_component: Component = Field(description="Where it starts; 'unknown' if the "
                                                         "evidence cannot establish it.")
    causal_chain: list[ClaimOut]
    proposed_fix: str
    evidence_gaps: list[str]
