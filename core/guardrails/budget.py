"""Hard call, cost, and wall-clock budgets for the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


class BudgetExceeded(RuntimeError):
    def __init__(self, dimension: str) -> None:
        super().__init__(f"agent budget exceeded: {dimension}")
        self.dimension = dimension


@dataclass(frozen=True)
class BudgetLimits:
    max_llm_calls: int = 8
    max_tool_calls: int = 24
    max_cost_usd: float = 0.12
    max_wall_seconds: float = 20.0


class BudgetLedger:
    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or BudgetLimits()
        self.started_at = monotonic()
        self.llm_calls = 0
        self.tool_calls = 0
        self.cost_usd = 0.0

    @property
    def elapsed_seconds(self) -> float:
        return monotonic() - self.started_at

    def check_time(self) -> None:
        if self.elapsed_seconds > self.limits.max_wall_seconds:
            raise BudgetExceeded("wall_clock")

    def reserve_llm_call(self, estimated_cost_usd: float = 0.0) -> None:
        self.check_time()
        if self.llm_calls + 1 > self.limits.max_llm_calls:
            raise BudgetExceeded("llm_calls")
        if self.cost_usd + estimated_cost_usd > self.limits.max_cost_usd:
            raise BudgetExceeded("cost")
        self.llm_calls += 1
        self.cost_usd += estimated_cost_usd

    def reserve_tool_call(self) -> None:
        self.check_time()
        if self.tool_calls + 1 > self.limits.max_tool_calls:
            raise BudgetExceeded("tool_calls")
        self.tool_calls += 1
