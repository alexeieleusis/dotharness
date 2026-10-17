import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.config import AutoApproveConfig, HarnessConfig, HarnessSection, RepoConfig, VibehealConfig
from harness.runners import auto_approve
from harness.runners.auto_approve_github import (
    ApprovalResult,
    ApprovalStatus,
    GateResult,
    GateStatus,
    SafetyResult,
    SafetyStatus,
    ViewedResult,
    ViewedStatus,
)
from harness.runners.auto_approve_size import SizeEvaluation
from harness.runners.common import FatalGitError

PR = {
    "number": 1,
    "headRefName": "feat",
    "baseRefName": "main",
    "isDraft": False,
    "state": "OPEN",
    "author": {"login": "alice"},
}


def _cfg(tmp_path, enabled=True, viewed=75, approve=500):
    return HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        vibe_heal=VibehealConfig(),
        auto_approve=AutoApproveConfig(enabled=enabled, viewed_max_lines=viewed, approve_max_lines=approve),
    )


def _size(counts=None, approve=500, viewed=75):
    counts = {"a.py": 10, "b.py": 100} if counts is None else counts
    return SizeEvaluation(
        file_counts=counts,
        total_lines=sum(counts.values()),
        small_files=[p for p, n in counts.items() if n < viewed],
        pr_is_small=sum(counts.values()) < approve,
    )


@contextlib.contextmanager
def _mocks(prs=None, size=None):
    prs = [PR] if prs is None else prs
    list_result = SimpleNamespace(returncode=0, stdout=json.dumps(prs).encode())
    p = "harness.runners.auto_approve."
    with (
        patch(p + "get_gh_token", return_value="tok") as gh_token,
        patch(p + "build_subprocess_env", return_value={}),
        patch(p + "get_current_user", return_value="me") as cur,
        patch(p + "run_cmd", return_value=list_result) as run_cmd,
        patch(p + "git_detach_and_record", return_value="orig"),
        patch(p + "_checkout_pr_head", return_value="sha1") as checkout,
        patch(p + "git_restore") as restore,
        patch(p + "evaluate_size", return_value=size or _size()) as ev,
        patch(p + "check_timing_gate", return_value=GateResult(GateStatus.PASSED, "zero-findings")) as gate,
        patch(p + "check_approval_safety", return_value=SafetyResult(SafetyStatus.SAFE, "ok", "sha1")) as safety,
        patch(p + "mark_files_viewed", return_value=ViewedResult(ViewedStatus.DONE, marked=["a.py"])) as mark,
        patch(p + "approve_pr", return_value=ApprovalResult(ApprovalStatus.APPROVED, "approved")) as approve,
    ):
        yield SimpleNamespace(
            gh_token=gh_token,
            cur=cur,
            run_cmd=run_cmd,
            checkout=checkout,
            restore=restore,
            ev=ev,
            gate=gate,
            safety=safety,
            mark=mark,
            approve=approve,
        )


def test_disabled_is_noop_with_zero_gh_calls(tmp_path):
    with _mocks() as m:
        auto_approve.run(_cfg(tmp_path, enabled=False))
    m.gh_token.assert_not_called()
    m.cur.assert_not_called()
    m.run_cmd.assert_not_called()
    m.checkout.assert_not_called()
    m.mark.assert_not_called()
    m.approve.assert_not_called()


def test_no_requested_prs_logs_a_summary(tmp_path, caplog):
    with _mocks(prs=[]) as m, caplog.at_level(logging.INFO):
        auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()
    assert "no open PRs with a pending review request" in caplog.text


def test_draft_closed_merged_skipped(tmp_path):
    prs = [
        {**PR, "number": 1, "isDraft": True},
        {**PR, "number": 2, "state": "CLOSED"},
        {**PR, "number": 3, "state": "MERGED"},
    ]
    with _mocks(prs=prs) as m:
        auto_approve.run(_cfg(tmp_path))
    m.checkout.assert_not_called()
    m.mark.assert_not_called()
    m.approve.assert_not_called()


def test_gate_closed_means_no_writes(tmp_path):
    with _mocks() as m:
        m.gate.return_value = GateResult(GateStatus.NOT_PASSED, "author-activity-after-T")
        m.mark.return_value = ViewedResult(ViewedStatus.GATE_NOT_PASSED)
        m.approve.return_value = ApprovalResult(ApprovalStatus.SKIPPED, "gate-not-passed")
        auto_approve.run(_cfg(tmp_path))
    assert m.mark.call_args.args[4] is False  # gate_passed
    assert m.approve.call_args.args[2].status is GateStatus.NOT_PASSED  # gate to approve_pr


def test_gate_open_marks_only_small_files_via_size_counts(tmp_path):
    with _mocks() as m:
        auto_approve.run(_cfg(tmp_path))
    args = m.mark.call_args.args
    assert args[0] == "acme/frontend"
    assert args[2] == {"a.py": 10, "b.py": 100}
    assert args[3] == 75
    assert args[4] is True


def test_approval_bound_to_head_sha_and_inputs(tmp_path):
    with _mocks() as m:
        auto_approve.run(_cfg(tmp_path))
    args = m.approve.call_args.args
    assert args[:2] == ("acme/frontend", 1)
    assert args[2].status is GateStatus.PASSED
    assert args[3] == SafetyResult(SafetyStatus.SAFE, "ok", "sha1")
    assert args[4].total_lines == 110
    assert args[5:] == (500, "me", {}, False)


def test_safety_skip_still_marks_but_does_not_pass_safety(tmp_path):
    with _mocks() as m:
        m.safety.return_value = SafetyResult(SafetyStatus.SKIP, "ci-blocked", "sha1")
        auto_approve.run(_cfg(tmp_path))
    m.mark.assert_called_once()
    assert m.approve.call_args.args[3].status is SafetyStatus.SKIP


def test_threshold_zero_never_matches(tmp_path):
    with _mocks(size=_size(viewed=0, approve=0)) as m:
        auto_approve.run(_cfg(tmp_path, viewed=0, approve=0))
    assert m.mark.call_args.args[3] == 0  # nothing is below 0, so mark_files_viewed marks nothing
    assert m.approve.call_args.args[5] == 0
    assert m.approve.call_args.kwargs["all_viewed"] is False


def test_empty_pr_not_approved_and_logged(tmp_path, caplog):
    with _mocks(size=_size(counts={})) as m, caplog.at_level(logging.INFO):
        auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()
    assert "no counted files" in caplog.text
    m.restore.assert_called_once()


def test_all_files_viewed_passes_flag_to_approve(tmp_path, caplog):
    with _mocks() as m, caplog.at_level(logging.INFO):
        m.mark.return_value = ViewedResult(ViewedStatus.DONE, all_viewed=True)
        auto_approve.run(_cfg(tmp_path))
    assert m.approve.call_args.kwargs["all_viewed"] is True
    assert "size limit waived" in caplog.text


def test_all_viewed_large_pr_still_goes_through_gate_and_safety(tmp_path):
    with _mocks(size=_size(counts={"a.py": 900})) as m:
        m.mark.return_value = ViewedResult(ViewedStatus.DONE, all_viewed=True)
        auto_approve.run(_cfg(tmp_path))
    args = m.approve.call_args.args
    assert args[2].status is GateStatus.PASSED and args[3].status is SafetyStatus.SAFE
    assert args[4].total_lines == 900


def test_all_viewed_empty_pr_is_approved(tmp_path):
    with _mocks(size=_size(counts={})) as m:
        m.mark.return_value = ViewedResult(ViewedStatus.DONE, all_viewed=True)
        auto_approve.run(_cfg(tmp_path))
    m.approve.assert_called_once()


def test_runner_passes_identical_writer_args_on_each_run(tmp_path):
    # Idempotency itself lives in auto_approve_github (see test_already_viewed_skipped_and_rerun_no_write
    # and test_already_approved_on_head_skips); here we only pin that the runner feeds the writers the
    # same inputs on every run, so those guards can do their job.
    with _mocks() as m:
        auto_approve.run(_cfg(tmp_path))
        auto_approve.run(_cfg(tmp_path))
    assert m.mark.call_count == 2 and m.approve.call_count == 2
    assert m.mark.call_args_list[0] == m.mark.call_args_list[1]
    assert m.approve.call_args_list[0] == m.approve.call_args_list[1]


@pytest.mark.parametrize("which", ["gate", "safety"])
def test_read_failure_means_zero_writes_and_raises(tmp_path, which):
    with _mocks() as m:
        if which == "gate":
            m.gate.return_value = GateResult(GateStatus.READ_FAILED, "boom")
        else:
            m.safety.return_value = SafetyResult(SafetyStatus.READ_FAILED, "boom", "sha1")
        with pytest.raises(auto_approve.AutoApproveError, match="#1"):
            auto_approve.run(_cfg(tmp_path))
    m.mark.assert_not_called()
    m.approve.assert_not_called()


def test_viewed_read_failure_blocks_approval_and_raises(tmp_path):
    with _mocks() as m:
        m.mark.return_value = ViewedResult(ViewedStatus.READ_FAILED)
        with pytest.raises(auto_approve.AutoApproveError):
            auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()


def test_write_failure_raises_after_loop(tmp_path):
    with _mocks() as m:
        m.approve.return_value = ApprovalResult(ApprovalStatus.WRITE_FAILED, "post failed")
        with pytest.raises(auto_approve.AutoApproveError, match="post failed"):
            auto_approve.run(_cfg(tmp_path))


def test_one_pr_failing_does_not_stop_others_and_raises(tmp_path):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}, {**PR, "number": 3}]
    with _mocks(prs=prs) as m:
        m.ev.side_effect = [RuntimeError("git exploded"), _size(), _size()]
        with pytest.raises(auto_approve.AutoApproveError) as exc:
            auto_approve.run(_cfg(tmp_path))
    assert "#1" in str(exc.value)
    assert "git exploded" in str(exc.value)
    assert [c.args[1] for c in m.approve.call_args_list] == [2, 3]
    assert m.restore.call_count == 3


def test_fatal_git_error_stops_loop_and_skips_remaining_prs(tmp_path):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}, {**PR, "number": 3}]
    with _mocks(prs=prs) as m:
        m.ev.side_effect = [FatalGitError("tree dirty"), _size(), _size()]
        with pytest.raises(auto_approve.AutoApproveError) as exc:
            auto_approve.run(_cfg(tmp_path))
    assert "#1" in str(exc.value)
    assert "tree dirty" in str(exc.value)
    assert "#2" not in str(exc.value)
    m.approve.assert_not_called()
    assert m.checkout.call_count == 1
    assert m.restore.call_count == 1


def test_fatal_restore_failure_keeps_approval_logged_and_stops_loop(tmp_path, caplog):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}]
    with _mocks(prs=prs) as m, caplog.at_level(logging.INFO):
        m.restore.side_effect = FatalGitError("restore failed")
        with pytest.raises(auto_approve.AutoApproveError, match="#1: restore failed"):
            auto_approve.run(_cfg(tmp_path))
    assert [c.args[1] for c in m.approve.call_args_list] == [1]
    assert "PR #1: approval approved" in caplog.text
    assert m.checkout.call_count == 1  # PR 2 never checked out onto an unknown tree


@pytest.mark.parametrize("author", [None, {}, {"login": ""}])
def test_missing_author_reaches_gate_as_empty_and_blocks_writes(tmp_path, author):
    # Deleted accounts come back from gh as null; the real gate treats an empty author as READ_FAILED.
    with _mocks(prs=[{**PR, "author": author}]) as m:
        m.gate.return_value = GateResult(GateStatus.READ_FAILED, "acting account or PR author unknown")
        with pytest.raises(auto_approve.AutoApproveError, match="#1"):
            auto_approve.run(_cfg(tmp_path))
    assert m.gate.call_args.args[2] == ""
    m.mark.assert_not_called()
    m.approve.assert_not_called()


def test_pr_list_failure_raises(tmp_path):
    with _mocks() as m:
        m.run_cmd.return_value = SimpleNamespace(returncode=1, stdout=b"")
        with pytest.raises(auto_approve.AutoApproveError):
            auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()


def test_dry_run_passes_flag_to_writers_and_logs(tmp_path, caplog):
    with _mocks() as m, caplog.at_level(logging.INFO):
        m.approve.return_value = ApprovalResult(ApprovalStatus.DRY_RUN, "dry-run")
        auto_approve.run(_cfg(tmp_path), dry_run=True)
    assert m.mark.call_args.kwargs["dry_run"] is True
    assert m.approve.call_args.args[8] is True
    assert "approval dry_run" in caplog.text


def test_each_decision_is_logged(tmp_path, caplog):
    with _mocks() as m, caplog.at_level(logging.INFO):
        auto_approve.run(_cfg(tmp_path))
    text = caplog.text
    for needle in (
        "total_lines=110",
        "approve_max_lines=500",
        "timing gate passed",
        "safety safe",
        "viewed done",
        "approval approved",
    ):
        assert needle in text, needle
    assert m.restore.call_count == 1


def test_module_docstring_documents_signature():
    assert "run(config: HarnessConfig, *, dry_run: bool = False) -> None" in (auto_approve.__doc__ or "")


def test_head_moved_since_size_measured_means_no_writes(tmp_path, caplog):
    with _mocks() as m:
        m.checkout.return_value = "old-sha"  # safety reports "sha1"
        with caplog.at_level(logging.WARNING):
            auto_approve.run(_cfg(tmp_path))
    m.mark.assert_not_called()
    m.approve.assert_not_called()
    assert "head moved" in caplog.text


def test_fork_pr_skipped(tmp_path):
    with _mocks(prs=[{**PR, "isCrossRepository": True}]) as m:
        auto_approve.run(_cfg(tmp_path))
    m.checkout.assert_not_called()
    m.approve.assert_not_called()
