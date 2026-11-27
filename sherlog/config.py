"""Runtime configuration for the engine. One place, read once.

Model IDs are pinned to dated snapshots, never floating aliases: a provider silently moving
the model behind an alias mid-project would invalidate every accuracy number, and nothing
would tell us. The model ID is an experimental variable and is recorded on every run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# LangSmith picks these up from the environment. Tracing is on whenever a key is present.
if os.environ.get("LANGSMITH_API_KEY"):
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", "sherlog")


@dataclass(frozen=True)
class Models:
    fast: str = os.environ.get("SHERLOG_MODEL_FAST", "gpt-4.1-mini-2025-04-14")
    balanced: str = os.environ.get("SHERLOG_MODEL_BALANCED", "gpt-4.1-2025-04-14")
    reasoning: str = os.environ.get("SHERLOG_MODEL_REASONING", "gpt-4.1-2025-04-14")

    def for_role(self, role: str) -> str:
        return {"fast": self.fast, "balanced": self.balanced, "reasoning": self.reasoning}[role]


#: USD per 1M tokens (input, output). Checked against OpenAI's published pricing at build
#: time; re-verify before quoting cost numbers.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1-2025-04-14": (2.00, 8.00),
    "gpt-4.1-mini-2025-04-14": (0.40, 1.60),
}


@dataclass(frozen=True)
class Endpoints:
    prometheus: str = os.environ.get("SHERLOG_PROMETHEUS", "http://localhost:9090")
    loki: str = os.environ.get("SHERLOG_LOKI", "http://localhost:3100")
    tempo: str = os.environ.get("SHERLOG_TEMPO", "http://localhost:3200")
    # Role `agent`: SELECT on the application database, no grant at all on sherlog_truth.
    app_db: str = os.environ.get(
        "SHERLOG_APP_DSN", "postgresql://agent:agent@localhost:5432/nimbus"
    )
    deploy_repo: Path = ROOT / "runtime" / "deploy-repo"
    compose_project: str = "nimbus"


@dataclass(frozen=True)
class Settings:
    models: Models = field(default_factory=Models)
    endpoints: Endpoints = field(default_factory=Endpoints)
    investigations_dir: Path = ROOT / "runtime" / "investigations"
    runbooks_dir: Path = ROOT / "docs" / "runbooks"
    seed: int = 7


SETTINGS = Settings()

#: Services the engine may name. Anything else in a report is an invented entity.
COMPONENTS = (
    "gateway", "orders-svc", "payments-svc", "psp-stub", "worker", "redis", "postgres",
)

TOPOLOGY = """\
k6 load -> gateway (public edge, owns timeouts/retries, no DB)
gateway -> orders-svc (HTTP)
orders-svc -> postgres (connection pool; orders, jobs, catalog, price_history tables)
orders-svc -> redis (catalog/price cache, via a network proxy)
orders-svc -> payments-svc (HTTP; called while a DB connection is still held)
payments-svc -> psp-stub (external payment provider, via a network proxy)
worker -> postgres (polls the jobs table, marks orders COMPLETE)
SLOs at the gateway: availability (non-5xx /checkout), latency (/checkout under threshold),
freshness (orders COMPLETE within 60s of creation)."""

#: The same call graph as data: which components each one calls. Must match TOPOLOGY.
DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "gateway": ("orders-svc",),
    "orders-svc": ("postgres", "redis", "payments-svc"),
    "payments-svc": ("psp-stub",),
    "worker": ("postgres",),
}


def dependencies(component: str) -> set[str]:
    """Every component ``component`` calls, directly or transitively."""
    out: set[str] = set()
    stack = list(DEPENDS_ON.get(component, ()))
    while stack:
        c = stack.pop()
        if c not in out:
            out.add(c)
            stack.extend(DEPENDS_ON.get(c, ()))
    return out
