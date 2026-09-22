from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import REPO as _REPO
from conftest import as_mock as _m
from conftest import make_completion_record as _record
from conftest import make_config as _config
from conftest import make_phase as _phase
from conftest import make_toolchain as _toolchain

from spec_prism_flow.build import phase_runner
from spec_prism_flow.build.errors import (
    EmptyImplementationError,
    ManualTestFailed,
    RetryBudgetExhausted,
    ScopeViolation,
)
from spec_prism_flow.build.gh_ops import PRStatus
from spec_prism_flow.build.git_ops import GitCommandError
from spec_prism_flow.build.manual_test import ManualTestOutcome
from spec_prism_flow.build.phase_runner import PhaseRunResult, run_phase
from spec_prism_flow.build.resume_state import ResumeState, load_resume_state, resume_state_path, save_resume_state
from spec_prism_flow.build.toolchain import build_dry_run_toolchain
from spec_prism_flow.config import BuildConfig, ReviewConfig, VibeHealConfig


def _state_path(config, phase) -> Path:
    branch = f"phase-{phase.number:02d}-{phase.name}"
    return resume_state_path(config, _REPO, branch)


# --- repo_slug ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin_url_value", "expected"),
    [
        ("git@github.com:acme/widget.git", "acme/widget"),
        ("https://github.com/acme/widget.git", "acme/widget"),
        ("https://github.com/acme/widget", "acme/widget"),
    ],
    ids=["ssh-style", "https-style", "missing-dot-git-suffix"],
)
def test_repo_slug_parses_origin_url(tmp_path, monkeypatch, origin_url_value, expected):
    monkeypatch.setattr(phase_runner, "origin_url", Mock(return_value=origin_url_value))
    assert phase_runner.repo_slug(tmp_path) == expected


# --- phase_status ---------------------------------------------------------------------


def test_phase_status_merged_when_record_has_pr_merged_at(tmp_path):
    record = _record(pr_merged_at=datetime(2026, 1, 2, tzinfo=UTC))
    assert phase_runner.phase_status(record, tmp_path / "missing-state.json") == "merged"


def test_phase_status_escalated_when_record_has_escalation_reason_and_no_merge(tmp_path):
    record = _record(pr_merged_at=None, escalation_reason="boom")
    assert phase_runner.phase_status(record, tmp_path / "missing-state.json") == "escalated"


def test_phase_status_in_progress_when_no_record_but_state_path_exists(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    assert phase_runner.phase_status(None, state_path) == "in progress"


def test_phase_status_pending_when_no_record_and_no_state_path(tmp_path):
    assert phase_runner.phase_status(None, tmp_path / "missing-state.json") == "pending"


# --- Happy path: all 8 steps ----------------------------------------------------------


def test_run_phase_happy_path_runs_all_steps_in_order_and_merges(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.checkout_fresh_branch).assert_called_once_with(
        clone, "phase-11-build-phase-run-orchestrator-leaf", "main"
    )
    _m(toolchain.agent_run).assert_called_once()
    _m(toolchain.commit_all).assert_any_call(clone, "phase 11: build-phase-run-orchestrator-leaf")
    _m(toolchain.scope_check).assert_any_call(["spec_prism_flow/build/toolchain.py"], phase.scope, "origin/main")
    _m(toolchain.push_branch).assert_any_call(clone, "phase-11-build-phase-run-orchestrator-leaf")
    _m(toolchain.pr_create).assert_called_once()
    _m(toolchain.manual_test_prompt).assert_called_once_with(11, phase.name, phase.manual_test_checklist, False)
    _m(toolchain.merge_gates_run).assert_called_once_with(clone, ["true"])
    _m(toolchain.pr_merge).assert_called_once_with(_REPO, 42)
    _m(toolchain.completion_log_append).assert_called_once()

    assert isinstance(result, PhaseRunResult)
    assert result.merged is True
    assert result.phase_number == 11
    record = result.completion_record
    assert record.pr_number == 42
    assert record.pr_url == "https://github.com/acme/widget/pull/42"
    assert record.pr_merged_at is not None
    assert record.manual_test_first_try_pass is True
    assert record.pr_diff_files == 1
    assert record.pr_diff_lines_added == 5
    assert record.pr_diff_lines_removed == 2
    assert record.escalation_reason is None
    assert record.human_escalations == 0


def test_run_phase_clears_resume_state_after_merge(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain)

    assert load_resume_state(_state_path(config, phase)) is None


def test_run_phase_completion_log_append_git_error_after_merge_does_not_propagate(tmp_path):
    """A push conflict that outlives `completion_log_append`'s own retries must not
    surface as an uncaught `GitCommandError`, and must not leave `resume_state`
    pointing at the now-merged PR -- both would make a later `--resume` run treat an
    already-merged phase as unfinished."""
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(
        completion_log_append=Mock(side_effect=GitCommandError(["git", "push"], 1, "conflict")),
    )
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain)

    assert result.merged is True
    assert load_resume_state(_state_path(config, phase)) is None


def test_run_phase_pr_created_with_a_title_and_body_derived_from_the_phase(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain)

    args = _m(toolchain.pr_create).call_args.args
    assert args[0] == clone
    assert args[1] == "phase-11-build-phase-run-orchestrator-leaf"
    assert "11" in args[2]
    assert "## Scope" in args[3]


# --- Step 1: empty implementation ------------------------------------------------------


def test_run_phase_raises_empty_implementation_error_on_empty_commit(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(commit_all=Mock(return_value=False))
    clone = tmp_path / "clone"

    with pytest.raises(EmptyImplementationError):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.scope_check).assert_not_called()
    _m(toolchain.push_branch).assert_not_called()
    _m(toolchain.pr_create).assert_not_called()


def test_run_phase_empty_implementation_writes_partial_completion_log_with_no_pr(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(commit_all=Mock(return_value=False))
    clone = tmp_path / "clone"

    with pytest.raises(EmptyImplementationError):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.completion_log_append).assert_called_once()
    _, record = _m(toolchain.completion_log_append).call_args.args
    assert record.pr_number is None
    assert record.pr_merged_at is None
    assert record.human_escalations == 1
    assert "No implementation" in record.escalation_reason


# --- Step 1: scope violation -------------------------------------------------------------


def test_run_phase_scope_violation_prevents_push(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(
        scope_check=Mock(side_effect=ScopeViolation(["evil.py"], phase.scope, "origin/main")),
    )
    clone = tmp_path / "clone"

    with pytest.raises(ScopeViolation):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.push_branch).assert_not_called()
    _m(toolchain.completion_log_append).assert_called_once()


# --- Retry budget --------------------------------------------------------------------


def test_run_phase_retry_budget_checked_before_each_cycles_work(tmp_path):
    phase = _phase()
    config = _config(tmp_path, build=BuildConfig(state_dir=tmp_path / "state", max_retry_cycles=1, commands=["true"]))
    toolchain = _toolchain(unresolved_thread_count=Mock(return_value=1))
    clone = tmp_path / "clone"

    with pytest.raises(RetryBudgetExhausted) as exc_info:
        run_phase(clone, config, phase, toolchain=toolchain)

    # max_retry_cycles=1: cycles 0 and 1 pass the check and run; cycle_index=2 fails
    # the check *before* a third cycle's work runs.
    assert _m(toolchain.unresolved_thread_count).call_count == 2
    assert exc_info.value.cycles == 2
    _m(toolchain.manual_test_prompt).assert_not_called()


def test_run_phase_retry_budget_exhausted_writes_partial_completion_log(tmp_path):
    phase = _phase()
    config = _config(tmp_path, build=BuildConfig(state_dir=tmp_path / "state", max_retry_cycles=0, commands=["true"]))
    toolchain = _toolchain(unresolved_thread_count=Mock(return_value=1))
    clone = tmp_path / "clone"

    with pytest.raises(RetryBudgetExhausted):
        run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.completion_log_append).assert_called_once()
    _, record = _m(toolchain.completion_log_append).call_args.args
    assert "Retry budget exhausted" in record.escalation_reason
    assert record.pr_number == 42


# --- Manual test outcomes -----------------------------------------------------------------


def test_run_phase_manual_test_retry_loops_back_and_consumes_another_cycle(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(
        manual_test_prompt=Mock(
            side_effect=[
                ManualTestOutcome(passed=False, retry=True, notes="try again"),
                ManualTestOutcome(passed=True, retry=False, notes=None),
            ]
        ),
    )
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain)

    assert _m(toolchain.manual_test_prompt).call_count == 2
    assert _m(toolchain.fetch_resync).call_count == 3  # step 2's + two iterate-loop cycles
    assert result.merged is True
    assert result.completion_record.manual_test_first_try_pass is False  # from the *first* outcome only


def test_run_phase_manual_test_non_strict_terminal_failure_proceeds_to_merge(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(
        manual_test_prompt=Mock(return_value=ManualTestOutcome(passed=False, retry=False, notes="nope")),
    )
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain, strict=False)

    assert result.merged is True
    assert result.completion_record.manual_test_first_try_pass is False
    _m(toolchain.pr_merge).assert_called_once()


def test_run_phase_manual_test_strict_failure_raises_and_never_merges(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(manual_test_prompt=Mock(side_effect=ManualTestFailed("nope")))
    clone = tmp_path / "clone"

    with pytest.raises(ManualTestFailed):
        run_phase(clone, config, phase, toolchain=toolchain, strict=True)

    _m(toolchain.pr_merge).assert_not_called()
    _m(toolchain.completion_log_append).assert_called_once()


def test_run_phase_threads_strict_through_to_manual_test_prompt(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain, strict=True)

    _m(toolchain.manual_test_prompt).assert_called_once_with(11, phase.name, phase.manual_test_checklist, True)


# --- Unresolved review threads ------------------------------------------------------------


def test_run_phase_unresolved_threads_loop_without_running_manual_test(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(unresolved_thread_count=Mock(side_effect=[2, 0]))
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain)

    assert _m(toolchain.unresolved_thread_count).call_count == 2
    assert _m(toolchain.manual_test_prompt).call_count == 1
    assert result.merged is True


# --- auto_merge=False -----------------------------------------------------------------------


def test_run_phase_auto_merge_false_stops_before_merging(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain, auto_merge=False)

    assert result.merged is False
    assert result.completion_record.pr_merged_at is None
    _m(toolchain.pr_merge).assert_not_called()
    _m(toolchain.completion_log_append).assert_not_called()
    _m(toolchain.merge_gates_run).assert_called_once()


def test_run_phase_auto_merge_false_leaves_resume_state_in_place(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain, auto_merge=False)

    assert load_resume_state(_state_path(config, phase)) is not None


# --- resume ------------------------------------------------------------------------------


def test_run_phase_resume_with_open_pr_skips_implement_and_reuses_pr(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    branch = f"phase-{phase.number:02d}-{phase.name}"
    save_resume_state(
        _state_path(config, phase),
        ResumeState(
            repo=_REPO, branch=branch, pr_number=99, pr_url="https://github.com/acme/widget/pull/99", cycle_index=1
        ),
    )
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain, resume=True)

    _m(toolchain.checkout_fresh_branch).assert_not_called()
    _m(toolchain.agent_run).assert_not_called()
    _m(toolchain.pr_create).assert_not_called()
    _m(toolchain.pr_view).assert_called_once_with(_REPO, 99)
    assert result.completion_record.pr_number == 99
    assert result.completion_record.pr_url == "https://github.com/acme/widget/pull/99"


def test_run_phase_resume_seeds_retry_budget_from_persisted_cycle_index(tmp_path):
    phase = _phase()
    config = _config(tmp_path, build=BuildConfig(state_dir=tmp_path / "state", max_retry_cycles=2, commands=["true"]))
    branch = f"phase-{phase.number:02d}-{phase.name}"
    save_resume_state(
        _state_path(config, phase),
        ResumeState(
            repo=_REPO, branch=branch, pr_number=99, pr_url="https://github.com/acme/widget/pull/99", cycle_index=3
        ),
    )
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    with pytest.raises(RetryBudgetExhausted) as exc_info:
        run_phase(clone, config, phase, toolchain=toolchain, resume=True)

    # cycle_index=3 seeded from resume state; max_retry_cycles=2 -> 3 > 2 raises
    # immediately, before any iterate-loop cycle work runs.
    assert exc_info.value.cycles == 3
    _m(toolchain.fetch_resync).assert_not_called()
    _m(toolchain.unresolved_thread_count).assert_not_called()


def test_run_phase_resume_true_but_pr_no_longer_open_runs_fresh_implement(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    branch = f"phase-{phase.number:02d}-{phase.name}"
    save_resume_state(
        _state_path(config, phase),
        ResumeState(
            repo=_REPO, branch=branch, pr_number=99, pr_url="https://github.com/acme/widget/pull/99", cycle_index=1
        ),
    )
    toolchain = _toolchain(pr_view=Mock(return_value=PRStatus(state="CLOSED", mergeable="UNKNOWN", review_decision="")))
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain, resume=True)

    _m(toolchain.checkout_fresh_branch).assert_called_once()
    _m(toolchain.pr_create).assert_called_once()
    assert result.completion_record.pr_number == 42  # the freshly created PR, not 99


def test_run_phase_resume_true_with_no_persisted_state_runs_fresh_implement(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain, resume=True)

    _m(toolchain.pr_view).assert_not_called()
    _m(toolchain.checkout_fresh_branch).assert_called_once()


def test_run_phase_each_cycle_resaves_resume_state_with_its_cycle_index(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = _toolchain(
        manual_test_prompt=Mock(
            side_effect=[
                ManualTestOutcome(passed=False, retry=True, notes=None),
                ManualTestOutcome(passed=True, retry=False, notes=None),
            ]
        ),
    )
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain, auto_merge=False)

    state = load_resume_state(_state_path(config, phase))
    assert state is not None
    assert state.cycle_index == 2


# --- vibe-heal static analysis ----------------------------------------------------------


def test_run_phase_static_analysis_skipped_entirely_when_disabled(tmp_path):
    phase = _phase()
    config = _config(tmp_path, vibe_heal=VibeHealConfig(enabled=False, tool_dir=tmp_path / "vibe-heal"))
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.static_analysis_scan).assert_not_called()
    _m(toolchain.static_analysis_post).assert_not_called()


def test_run_phase_static_analysis_posts_once_then_dedupes_identical_findings_across_cycles(tmp_path):
    phase = _phase()
    config = _config(tmp_path, vibe_heal=VibeHealConfig(enabled=True, tool_dir=tmp_path / "vibe-heal"))
    report = {"issues": [{"rule": "R1", "file": "a.py", "line": 1, "message": "m", "on_changed_line": True}]}
    toolchain = _toolchain(
        static_analysis_scan=Mock(return_value=report),
        manual_test_prompt=Mock(
            side_effect=[
                ManualTestOutcome(passed=False, retry=True, notes=None),
                ManualTestOutcome(passed=True, retry=False, notes=None),
            ]
        ),
    )
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain)

    assert _m(toolchain.static_analysis_scan).call_count == 2
    _m(toolchain.static_analysis_post).assert_called_once()


def test_run_phase_static_analysis_posts_again_when_new_findings_appear(tmp_path):
    phase = _phase()
    config = _config(tmp_path, vibe_heal=VibeHealConfig(enabled=True, tool_dir=tmp_path / "vibe-heal"))
    first_report = {"issues": [{"rule": "R1", "file": "a.py", "line": 1, "message": "m", "on_changed_line": True}]}
    second_report = {
        "issues": [
            {"rule": "R1", "file": "a.py", "line": 1, "message": "m", "on_changed_line": True},
            {"rule": "R2", "file": "b.py", "line": 2, "message": "m2", "on_changed_line": True},
        ]
    }
    toolchain = _toolchain(
        static_analysis_scan=Mock(side_effect=[first_report, second_report]),
        manual_test_prompt=Mock(
            side_effect=[
                ManualTestOutcome(passed=False, retry=True, notes=None),
                ManualTestOutcome(passed=True, retry=False, notes=None),
            ]
        ),
    )
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain)

    assert _m(toolchain.static_analysis_post).call_count == 2


# --- review self-review / address-comments -------------------------------------------------


def test_run_phase_review_disabled_never_calls_review_toolchain_fields(tmp_path):
    phase = _phase()
    config = _config(tmp_path, review=ReviewConfig(enabled=False, tool_dir=tmp_path / "pr", harness_config=None))
    toolchain = _toolchain()
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.review_self_review).assert_not_called()
    _m(toolchain.review_address_comments).assert_not_called()


def test_run_phase_review_enabled_counts_address_comments_cycles_and_pushes_when_dirty(tmp_path):
    phase = _phase()
    config = _config(
        tmp_path, review=ReviewConfig(enabled=True, tool_dir=tmp_path / "pr", harness_config=tmp_path / "h.toml")
    )
    commit_all = Mock(side_effect=[True, True])  # step-1 implement commit, then one address-comments commit
    toolchain = _toolchain(commit_all=commit_all)
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, toolchain=toolchain)

    _m(toolchain.review_self_review).assert_called_once()
    _m(toolchain.review_address_comments).assert_called_once()
    assert result.completion_record.address_comments_cycles == 1
    assert _m(toolchain.push_branch).call_count == 2  # step 2's push + the address-comments push


def test_run_phase_review_enabled_skips_push_when_address_comments_left_nothing_to_commit(tmp_path):
    phase = _phase()
    config = _config(
        tmp_path, review=ReviewConfig(enabled=True, tool_dir=tmp_path / "pr", harness_config=tmp_path / "h.toml")
    )
    commit_all = Mock(side_effect=[True, False])
    toolchain = _toolchain(commit_all=commit_all)
    clone = tmp_path / "clone"

    run_phase(clone, config, phase, toolchain=toolchain)

    assert _m(toolchain.push_branch).call_count == 1  # only step 2's push


# --- dry run end-to-end ---------------------------------------------------------------------


def test_run_phase_dry_run_default_toolchain_completes_end_to_end(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    clone = tmp_path / "clone"

    result = run_phase(clone, config, phase, dry_run=True)

    assert result.merged is True
    assert result.completion_record.pr_number == 1


def test_run_phase_dry_run_empty_implementation_on_second_call_of_a_fresh_toolchain(tmp_path):
    phase = _phase()
    config = _config(tmp_path)
    toolchain = build_dry_run_toolchain()
    toolchain.commit_all(tmp_path, "burn the first True")  # consume the single True

    with pytest.raises(EmptyImplementationError):
        run_phase(tmp_path / "clone", config, phase, toolchain=toolchain)
