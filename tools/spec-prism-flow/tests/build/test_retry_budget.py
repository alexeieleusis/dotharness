import dataclasses

import pytest

from spec_prism_flow.build.errors import RetryBudgetExhausted
from spec_prism_flow.build.retry_budget import RetryBudget


def test_check_returns_none_when_cycles_used_is_within_budget():
    budget = RetryBudget(max_cycles=2)

    assert budget.check(1, pr_url="https://example.com/pr/1", unresolved_thread_count=0) is None
    assert budget.check(2, pr_url=None, unresolved_thread_count=0) is None


def test_check_raises_exactly_when_cycles_used_exceeds_max_cycles():
    budget = RetryBudget(max_cycles=2)

    with pytest.raises(RetryBudgetExhausted) as exc_info:
        budget.check(3, pr_url="https://example.com/pr/1", unresolved_thread_count=4)

    err = exc_info.value
    assert err.cycles == 3
    assert err.pr_url == "https://example.com/pr/1"
    assert err.unresolved_thread_count == 4


def test_check_does_not_mutate_the_budget():
    budget = RetryBudget(max_cycles=2)

    budget.check(1, pr_url=None, unresolved_thread_count=0)
    with pytest.raises(RetryBudgetExhausted):
        budget.check(5, pr_url=None, unresolved_thread_count=0)

    assert budget.max_cycles == 2


def test_retry_budget_is_frozen():
    budget = RetryBudget(max_cycles=2)

    with pytest.raises(dataclasses.FrozenInstanceError):
        budget.max_cycles = 5  # ty: ignore[invalid-assignment]
