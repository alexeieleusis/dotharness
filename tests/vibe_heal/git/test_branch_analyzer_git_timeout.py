"""Tests for BranchAnalyzer git subprocess hardening (timeout, no prompts)."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from git import Repo

from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.exceptions import GitOperationError


@pytest.fixture
def analyzer(tmp_path: Path) -> BranchAnalyzer:
    Repo.init(tmp_path)
    return BranchAnalyzer(tmp_path)


def test_timeout_raises_git_operation_error(analyzer: BranchAnalyzer) -> None:
    with (
        patch(
            "dotharness.vibe_heal.git.branch_analyzer.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1),
        ),
        pytest.raises(GitOperationError, match="timed out"),
    ):
        analyzer.is_ancestor_of_head("main")


def test_git_runs_without_prompts_or_stdin(analyzer: BranchAnalyzer) -> None:
    with patch(
        "dotharness.vibe_heal.git.branch_analyzer.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
    ) as mock_run:
        analyzer.is_ancestor_of_head("main")

    kwargs = mock_run.call_args.kwargs
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] > 0
