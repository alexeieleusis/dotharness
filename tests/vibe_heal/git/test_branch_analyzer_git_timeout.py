"""Tests for BranchAnalyzer git subprocess hardening (timeout, no prompts)."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from git import Repo

from dotharness.subprocess_runner import RunResult
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.exceptions import GitOperationError


@pytest.fixture
def analyzer(tmp_path: Path) -> BranchAnalyzer:
    Repo.init(tmp_path)
    return BranchAnalyzer(tmp_path)


async def test_timeout_raises_git_operation_error(analyzer: BranchAnalyzer) -> None:
    timed_out = RunResult(returncode=124, stdout="", stderr="timed out after 1s", timed_out=True)
    with (
        patch("dotharness.vibe_heal.git.branch_analyzer.run", AsyncMock(return_value=timed_out)),
        pytest.raises(GitOperationError, match="timed out"),
    ):
        await analyzer.is_ancestor_of_head("main")


async def test_git_runs_without_prompts(analyzer: BranchAnalyzer) -> None:
    with patch(
        "dotharness.vibe_heal.git.branch_analyzer.run", AsyncMock(return_value=RunResult(0, "", ""))
    ) as mock_run:
        await analyzer.is_ancestor_of_head("main")

    kwargs = mock_run.call_args.kwargs
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["timeout"] > 0
