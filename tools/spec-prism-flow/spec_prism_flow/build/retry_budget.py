from __future__ import annotations

from dataclasses import dataclass

from spec_prism_flow.build.errors import RetryBudgetExhausted


@dataclass(frozen=True)
class RetryBudget:
    max_cycles: int

    def check(self, cycles_used: int, *, pr_url: str | None, unresolved_thread_count: int) -> None:
        if cycles_used > self.max_cycles:
            raise RetryBudgetExhausted(cycles_used, pr_url, unresolved_thread_count)
