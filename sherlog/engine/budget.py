"""Budget governor — hard caps checked before every model call and every tool call.

Breaching any cap exits through the ``budget`` branch of the outcome gate with partial
findings attached. It never degrades into a guess with less evidence behind it.

The dollar cap sits at roughly twice the estimated per-investigation cost, so a run that
doubles the estimate fails loudly instead of being absorbed as noise in the monthly bill.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


class BudgetExhausted(RuntimeError):
    def __init__(self, resource: str, used: float, cap: float) -> None:
        super().__init__(f"budget exhausted: {resource} {used:.4g} of {cap:.4g}")
        self.resource = resource
        self.used = used
        self.cap = cap


@dataclass
class Budget:
    max_tokens: int = 250_000
    # 17 snapshot + 1 doc + up to 5 hypotheses x (4 checks + 3 follow-ups) = 53 worst case.
    # The cap sits above that so a well-behaved run never ends on it; the $ cap stays primary.
    max_tool_calls: int = 60
    max_wall_clock_s: float = 480.0
    max_cost_usd: float = 0.60


@dataclass
class BudgetGovernor:
    budget: Budget = field(default_factory=Budget)
    tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    clock: Callable[[], float] = time.monotonic  # injectable for deterministic tests
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    @property
    def elapsed_s(self) -> float:
        return self.clock() - self.started_at

    def check(self) -> None:
        """Raise if any cap is already breached. Called before spending, not after."""
        b = self.budget
        if self.tokens >= b.max_tokens:
            raise BudgetExhausted("tokens", self.tokens, b.max_tokens)
        if self.tool_calls >= b.max_tool_calls:
            raise BudgetExhausted("tool_calls", self.tool_calls, b.max_tool_calls)
        if self.cost_usd >= b.max_cost_usd:
            raise BudgetExhausted("cost_usd", self.cost_usd, b.max_cost_usd)
        elapsed = self.elapsed_s  # read the clock once; two reads is two different answers
        if elapsed >= b.max_wall_clock_s:
            raise BudgetExhausted("wall_clock_s", elapsed, b.max_wall_clock_s)

    def charge_llm(self, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self.check()
        self.tokens += tokens_in + tokens_out
        self.cost_usd += cost_usd

    def charge_tool(self) -> None:
        self.check()
        self.tool_calls += 1

    def snapshot(self) -> dict[str, float]:
        """What the run ledger records and the UI renders. Cost-awareness on screen is
        part of the product, not debug output."""
        return {
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
            "cost_usd": round(self.cost_usd, 4),
            "elapsed_s": round(self.elapsed_s, 2),
            "tokens_pct": round(100 * self.tokens / self.budget.max_tokens, 1),
            "cost_pct": round(100 * self.cost_usd / self.budget.max_cost_usd, 1),
        }
