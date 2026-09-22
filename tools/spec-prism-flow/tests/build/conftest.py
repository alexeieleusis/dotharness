"""Shared fixtures/builders for the phase_runner and toolchain test suites."""

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from spec_prism_flow.build import phase_runner
from spec_prism_flow.build.gh_ops import PRHandle, PRStatus
from spec_prism_flow.build.git_ops import DiffStat
from spec_prism_flow.build.manual_test import ManualTestOutcome
from spec_prism_flow.build.toolchain import Toolchain
from spec_prism_flow.config import (
    AgentConfig,
    BuildConfig,
    HarnessSection,
    PlanConfig,
    ReviewConfig,
    SpecPrismFlowConfig,
    VibeHealConfig,
)
from spec_prism_flow.phase_file import PhaseFile

ORIGIN_URL = "git@github.com:acme/widget.git"
REPO = "acme/widget"

_DEFAULT_DIFF_STAT = DiffStat(files=1, lines_added=5, lines_removed=2)


@pytest.fixture(autouse=True)
def _patch_origin_url(monkeypatch):
    monkeypatch.setattr(phase_runner, "origin_url", Mock(return_value=ORIGIN_URL))


def make_phase(**overrides) -> PhaseFile:
    fields = {
        "number": 11,
        "name": "build-phase-run-orchestrator-leaf",
        "scope": ["spec_prism_flow/build/toolchain.py", "spec_prism_flow/build/phase_runner.py"],
        "requirements": "Some requirements text.",
        "acceptance_criteria": ["Thing one works."],
        "manual_test_checklist": ["Run the tests."],
        "depends_on": "Phase 10 merged.",
    }
    fields.update(overrides)
    return PhaseFile(**fields)


def make_config(tmp_path, **overrides) -> SpecPrismFlowConfig:
    fields = {
        "agent": AgentConfig(),
        "plan": PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        "review": ReviewConfig(
            enabled=False, tool_dir=tmp_path / "pr-review", harness_config=tmp_path / ".harness.toml"
        ),
        "vibe_heal": VibeHealConfig(enabled=False, tool_dir=tmp_path / "vibe-heal"),
        "build": BuildConfig(state_dir=tmp_path / "state", max_retry_cycles=3, commands=["true"]),
        "harness": HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    }
    fields.update(overrides)
    return SpecPrismFlowConfig(**fields)


def make_toolchain(*, diff_stat_value: DiffStat = _DEFAULT_DIFF_STAT, **overrides) -> Toolchain:
    fields = {
        "checkout_fresh_branch": Mock(),
        "agent_run": Mock(return_value="agent output"),
        "commit_all": Mock(return_value=True),
        "diff_paths": Mock(return_value=["spec_prism_flow/build/toolchain.py"]),
        "scope_check": Mock(),
        "push_branch": Mock(),
        "fetch_resync": Mock(),
        "pr_create": Mock(return_value=PRHandle(number=42, url="https://github.com/acme/widget/pull/42")),
        "pr_view": Mock(return_value=PRStatus(state="OPEN", mergeable="MERGEABLE", review_decision="APPROVED")),
        "static_analysis_scan": Mock(return_value=None),
        "static_analysis_post": Mock(),
        "review_self_review": Mock(return_value=""),
        "review_address_comments": Mock(return_value=""),
        "unresolved_thread_count": Mock(return_value=0),
        "manual_test_prompt": Mock(return_value=ManualTestOutcome(passed=True, retry=False, notes=None)),
        "diff_stat": Mock(return_value=diff_stat_value),
        "merge_gates_run": Mock(),
        "pr_merge": Mock(),
        "completion_log_append": Mock(),
    }
    fields.update(overrides)
    return Toolchain(**fields)


def as_mock(fn: object) -> Mock:
    """`Toolchain` fields are typed as the dependency signature they wire (e.g.
    `Callable[[Path, str, str], None]`), so a test that hands one a `Mock` and later
    wants `.assert_called_with`/`.call_count` off the same attribute needs it narrowed
    back to `Mock` for the type checker -- the runtime object is unchanged."""
    return cast(Mock, fn)


def run_git(cwd, *args) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603, S607


def init_git_repo(tmp_path: Path, dirname: str = "repo") -> Path:
    clone = tmp_path / dirname
    clone.mkdir()
    run_git(clone, "init", "-q")
    run_git(clone, "config", "user.email", "test@example.com")
    run_git(clone, "config", "user.name", "Test")
    return clone


def git_commit(clone: Path, message: str) -> None:
    run_git(clone, "add", "-A")
    run_git(clone, "commit", "-q", "-m", message)
