"""A timed-out gh call fails closed (None) from every public ci_checks entry point."""

import subprocess

import pytest

from dotharness.pr_review.runners import ci_checks


@pytest.fixture(autouse=True)
def _gh_times_out(monkeypatch):
    async def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["gh"], 1)

    monkeypatch.setattr(ci_checks, "run_cmd", boom)


@pytest.mark.parametrize("stack_root", [True, False])
async def test_read_checks_returns_none_on_timeout(stack_root):
    assert (await ci_checks.read_checks("o/r", 1, "main", {}, stack_root=stack_root)) is None


async def test_repo_has_workflows_returns_none_on_timeout():
    assert (await ci_checks.repo_has_workflows("o/r", {})) is None
