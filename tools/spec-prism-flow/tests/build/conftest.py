"""Shared fixtures/builders for the phase_runner, toolchain, track_runner,
parallel_runner and build-CLI test suites."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from spec_prism_flow.build import phase_runner
from spec_prism_flow.build.completion_log import CompletionRecord, _record_to_dict
from spec_prism_flow.build.gh_ops import PRHandle, PRStatus
from spec_prism_flow.build.git_ops import DiffStat
from spec_prism_flow.build.manual_test import ManualTestOutcome
from spec_prism_flow.build.phase_runner import PhaseRunResult
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
from spec_prism_flow.phase_file import PhaseFile, phase_file_name, render_phase_file

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


def write_phase_file(phases_dir: Path, phase: PhaseFile) -> Path:
    """Renders `phase` to a real `<NN>-<slug>-leaf.md` file under `phases_dir` --
    used by track_runner/parallel_runner/build-CLI tests that need `parse_phase_file`
    to succeed on a fixture corpus, not just a bare filename to glob-match."""
    phases_dir.mkdir(parents=True, exist_ok=True)
    path = phases_dir / phase_file_name(phase.number, phase.name)
    path.write_text(render_phase_file(phase))
    return path


def make_completion_record(**overrides) -> CompletionRecord:
    fields = {
        "phase_number": 1,
        "phase_name": "some-leaf",
        "pr_number": 1,
        "pr_url": "https://github.com/acme/widget/pull/1",
        "pr_opened_at": datetime(2026, 1, 1, tzinfo=UTC),
        "pr_merged_at": datetime(2026, 1, 2, tzinfo=UTC),
        "manual_test_first_try_pass": True,
        "escalation_reason": None,
        "address_comments_cycles": 1,
        "pr_diff_files": 1,
        "pr_diff_lines_added": 5,
        "pr_diff_lines_removed": 2,
        "human_escalations": 0,
    }
    fields.update(overrides)
    return CompletionRecord(**fields)


def fake_phase_run_result(number: int, *, merged: bool = True) -> PhaseRunResult:
    return PhaseRunResult(
        phase_number=number, merged=merged, completion_record=make_completion_record(phase_number=number)
    )


def write_completion_log(path: Path, records: list[CompletionRecord]) -> None:
    """Writes `records` as a completion-log.json file at `path`, via
    completion_log.py's own serialization so a fixture log round-trips through
    `completion_log.load_all` exactly like a real one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([_record_to_dict(record) for record in records]))
