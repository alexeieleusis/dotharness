"""Exercises every `OrchestrationError` subclass at its correct step in `run_phase`,
confirming each triggers the documented best-effort partial completion-log write
(escalation reason + step reached) before the original error re-raises unchanged --
and that a non-`OrchestrationError` failure gets no such write."""

from unittest.mock import Mock

import pytest
from conftest import as_mock as _m
from conftest import make_config as _config
from conftest import make_phase as _phase
from conftest import make_toolchain as _make_toolchain

from spec_prism_flow.build.errors import (
    EmptyImplementationError,
    ManualTestFailed,
    MergeGateFailure,
    RetryBudgetExhausted,
    ScopeViolation,
)
from spec_prism_flow.build.gh_ops import PRNotMergeableError
from spec_prism_flow.build.git_ops import DiffStat, GitCommandError
from spec_prism_flow.build.phase_runner import run_phase
from spec_prism_flow.build.toolchain import Toolchain
from spec_prism_flow.config import BuildConfig

_ESCALATION_DIFF_STAT = DiffStat(files=3, lines_added=30, lines_removed=7)


def _toolchain(**overrides) -> Toolchain:
    return _make_toolchain(diff_stat_value=_ESCALATION_DIFF_STAT, **overrides)


def _escalation_record(toolchain: Toolchain):
    clone_arg, record = _m(toolchain.completion_log_append).call_args.args
    return clone_arg, record


# --- EmptyImplementationError: step 1, before any scope check or push ------------------


def test_empty_implementation_error_raised_before_scope_check_and_push(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(commit_all=Mock(return_value=False))
    clone = tmp_path / "clone"

    with pytest.raises(EmptyImplementationError):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.scope_check).assert_not_called()
    _m(toolchain.push_branch).assert_not_called()
    _m(toolchain.pr_create).assert_not_called()

    _, record = _escalation_record(toolchain)
    assert record.pr_number is None
    assert record.pr_url is None
    assert record.pr_opened_at is None
    assert record.pr_merged_at is None
    assert record.manual_test_first_try_pass is None
    assert record.pr_diff_files == 0
    assert record.human_escalations == 1
    assert "No implementation" in record.escalation_reason


# --- ScopeViolation: step 1's scope check, before push --------------------------------


def test_scope_violation_at_step_1_raised_before_push(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(scope_check=Mock(side_effect=ScopeViolation(["evil.py"], phase.scope, "origin/main")))
    clone = tmp_path / "clone"

    with pytest.raises(ScopeViolation):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.push_branch).assert_not_called()
    _m(toolchain.pr_create).assert_not_called()

    _, record = _escalation_record(toolchain)
    assert record.pr_number is None
    assert "evil.py" in record.escalation_reason


def test_scope_violation_in_iterate_loop_recheck_after_pr_already_created(tmp_path):
    """The scope re-check runs every cycle, not just at step 1 -- a violation
    surfacing there happens with the PR already open, unlike the step-1 case."""
    phase = _phase()
    config = _config(tmp_path)
    scope_check = Mock(side_effect=[None, ScopeViolation(["late.py"], phase.scope, "origin/main")])
    toolchain = _toolchain(scope_check=scope_check)
    clone = tmp_path / "clone"

    with pytest.raises(ScopeViolation):
        run_phase(clone, config, phase, toolchain=toolchain)

    assert scope_check.call_count == 2
    _m(toolchain.manual_test_prompt).assert_not_called()

    _, record = _escalation_record(toolchain)
    assert record.pr_number == 42  # the PR from step 2 is already known at this point
    assert "late.py" in record.escalation_reason


# --- MergeGateFailure: step 7, after diff_stat is already computed ----------------------


def test_merge_gate_failure_after_diff_stat_already_computed(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(merge_gates_run=Mock(side_effect=MergeGateFailure("pytest", "3 failed")))
    clone = tmp_path / "clone"

    with pytest.raises(MergeGateFailure):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.pr_merge).assert_not_called()

    _, record = _escalation_record(toolchain)
    assert record.pr_number == 42
    assert record.pr_diff_files == 3  # from the default toolchain's diff_stat, already run
    assert record.pr_diff_lines_added == 30
    assert record.pr_diff_lines_removed == 7
    assert "pytest" in record.escalation_reason


# --- RetryBudgetExhausted: iterate loop, before that cycle's work -----------------------


def test_retry_budget_exhausted_carries_cycles_pr_url_and_unresolved_count(tmp_path):
    phase = _phase()
    config = _config(tmp_path, build=BuildConfig(state_dir=tmp_path / "state", max_retry_cycles=0, commands=["true"]))
    toolchain = _toolchain(unresolved_thread_count=Mock(return_value=5))
    clone = tmp_path / "clone"

    with pytest.raises(RetryBudgetExhausted) as exc_info:
        run_phase(clone, config, phase, toolchain=toolchain)

    assert exc_info.value.cycles == 1
    assert exc_info.value.pr_url == "https://github.com/acme/widget/pull/42"
    assert exc_info.value.unresolved_thread_count == 5

    _m(toolchain.manual_test_prompt).assert_not_called()
    _m(toolchain.pr_merge).assert_not_called()

    _, record = _escalation_record(toolchain)
    assert record.pr_number == 42
    assert "Retry budget exhausted" in record.escalation_reason


# --- ManualTestFailed: iterate loop, strict + decline-retry -----------------------------


def test_manual_test_failed_raised_inside_iterate_loop_before_merge_gates(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(manual_test_prompt=Mock(side_effect=ManualTestFailed("regression found")))
    clone = tmp_path / "clone"

    with pytest.raises(ManualTestFailed):
        run_phase(clone, config, phase, toolchain=toolchain, strict=True)

    _m(toolchain.diff_stat).assert_not_called()
    _m(toolchain.merge_gates_run).assert_not_called()
    _m(toolchain.pr_merge).assert_not_called()

    _, record = _escalation_record(toolchain)
    assert record.pr_number == 42
    assert record.manual_test_first_try_pass is None  # prompt raised before returning an outcome
    assert "regression found" in record.escalation_reason


# --- PRNotMergeableError: step 8, after the unmerged record is already fully built -------


def test_pr_not_mergeable_error_at_step_8_after_diff_stat_and_merge_gates(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(pr_merge=Mock(side_effect=PRNotMergeableError(["gh", "pr", "merge"], 1, "conflicts", 42)))
    clone = tmp_path / "clone"

    with pytest.raises(PRNotMergeableError):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.merge_gates_run).assert_called_once()  # already ran before pr_merge

    _, record = _escalation_record(toolchain)
    assert record.pr_number == 42
    assert record.pr_diff_files == 3  # diff_stat already computed at step 7
    assert record.pr_merged_at is None
    assert "conflicts" in record.escalation_reason


# --- Best-effort semantics: own failure is swallowed, non-OrchestrationError untouched ---


def test_completion_log_append_failure_during_escalation_is_swallowed(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(
        commit_all=Mock(return_value=False),
        completion_log_append=Mock(side_effect=RuntimeError("log write also failed")),
    )
    clone = tmp_path / "clone"

    with pytest.raises(EmptyImplementationError):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.completion_log_append).assert_called_once()


def test_non_orchestration_error_propagates_with_no_completion_log_write(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(checkout_fresh_branch=Mock(side_effect=GitCommandError(["git", "checkout"], 1, "boom")))
    clone = tmp_path / "clone"

    with pytest.raises(GitCommandError):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.completion_log_append).assert_not_called()
