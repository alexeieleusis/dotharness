import contextlib
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dotharness.code_host import (
    Capability,
    ChangeRequestState,
    CodeHostError,
    CodeHostTimeoutError,
    UnsupportedCapabilityError,
)
from dotharness.pr_review.config import AutoApproveConfig, HarnessConfig, HarnessSection, RepoConfig, VibehealConfig
from dotharness.pr_review.runners import auto_approve
from dotharness.pr_review.runners.auto_approve_github import (
    ApprovalResult,
    ApprovalStatus,
    GateResult,
    GateStatus,
    SafetyResult,
    SafetyStatus,
    ViewedResult,
    ViewedStatus,
)
from dotharness.pr_review.runners.auto_approve_size import SizeEvaluation
from dotharness.pr_review.runners.common import FatalGitError
from tests.pr_review.runners.approve_support import ME, make_host, update_pr
from tests.pr_review.runners.runner_support import REPO

PR: dict = {"number": 1}  # overrides of the seeded change request fields; every PR has ME as requested reviewer


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


def _seed(host, prs):
    for spec in prs:
        number = spec["number"]
        host.add_change_request(REPO, head_reference="feat", number=number, author="alice", requested_reviewers=[ME])
        fields = {k: v for k, v in spec.items() if k != "number"}
        if fields:
            update_pr(host, number, **fields)


@contextlib.contextmanager
def _mocks(prs=None, size=None, host=None):
    prs = [PR] if prs is None else prs
    host = host or make_host()
    _seed(host, prs)
    scaffold = SimpleNamespace(host=host.code_host(), repo=REPO, env={})
    p = "dotharness.pr_review.runners.auto_approve."
    with (
        patch(p + "open_host", return_value=scaffold) as open_host,
        patch(p + "git_detach_and_record", return_value="orig"),
        patch(p + "_checkout_pr_head", return_value="sha1") as checkout,
        patch(p + "git_restore") as restore,
        patch("dotharness.pr_review.runners.common.git_clean_working_dir", return_value=True) as clean,
        patch("dotharness.pr_review.runners.common.git_is_dirty", return_value=False) as dirty,
        patch(p + "evaluate_size", return_value=size or _size()) as ev,
        patch(p + "check_timing_gate", return_value=GateResult(GateStatus.PASSED, "zero-findings")) as gate,
        patch(p + "check_approval_safety", return_value=SafetyResult(SafetyStatus.SAFE, "ok", "sha1")) as safety,
        patch(p + "mark_files_viewed", return_value=ViewedResult(ViewedStatus.DONE, marked=["a.py"])) as mark,
        patch(p + "approve_pr", return_value=ApprovalResult(ApprovalStatus.APPROVED, "approved")) as approve,
    ):
        yield SimpleNamespace(
            open_host=open_host,
            host=host,
            scaffold=scaffold,
            checkout=checkout,
            restore=restore,
            clean=clean,
            dirty=dirty,
            ev=ev,
            gate=gate,
            safety=safety,
            mark=mark,
            approve=approve,
        )


async def test_disabled_is_noop_with_zero_host_calls(tmp_path):
    with _mocks() as m:
        await auto_approve.run(_cfg(tmp_path, enabled=False))
    m.open_host.assert_not_called()
    assert m.host.calls == []
    m.checkout.assert_not_called()
    m.mark.assert_not_called()
    m.approve.assert_not_called()


async def test_no_requested_prs_logs_a_summary(tmp_path, caplog):
    with _mocks(prs=[]) as m, caplog.at_level(logging.INFO):
        await auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()
    assert "no open PRs with a pending review request" in caplog.text


async def test_draft_closed_merged_skipped(tmp_path):
    prs = [
        {**PR, "number": 1, "is_draft": True},
        {**PR, "number": 2, "state": ChangeRequestState.CLOSED},
        {**PR, "number": 3, "state": ChangeRequestState.MERGED},
    ]
    with _mocks(prs=prs) as m:
        await auto_approve.run(_cfg(tmp_path))
    m.checkout.assert_not_called()
    m.mark.assert_not_called()
    m.approve.assert_not_called()


async def test_gate_closed_means_no_writes(tmp_path):
    with _mocks() as m:
        m.gate.return_value = GateResult(GateStatus.NOT_PASSED, "author-activity-after-T")
        m.mark.return_value = ViewedResult(ViewedStatus.GATE_NOT_PASSED)
        m.approve.return_value = ApprovalResult(ApprovalStatus.SKIPPED, "gate-not-passed")
        await auto_approve.run(_cfg(tmp_path))
    assert m.mark.call_args.args[5] is False  # gate_passed
    assert m.approve.call_args.args[3].status is GateStatus.NOT_PASSED  # gate to approve_pr


async def test_gate_open_marks_only_small_files_via_size_counts(tmp_path):
    with _mocks() as m:
        await auto_approve.run(_cfg(tmp_path))
    args = m.mark.call_args.args
    assert args[:3] == (m.scaffold.host, REPO, 1)
    assert args[3] == {"a.py": 10, "b.py": 100}
    assert args[4] == 75
    assert args[5] is True


async def test_approval_bound_to_head_sha_and_inputs(tmp_path):
    with _mocks() as m:
        await auto_approve.run(_cfg(tmp_path))
    args = m.approve.call_args.args
    assert args[:3] == (m.scaffold.host, REPO, 1)
    assert args[3].status is GateStatus.PASSED
    assert args[4] == SafetyResult(SafetyStatus.SAFE, "ok", "sha1")
    assert args[5].total_lines == 110
    assert args[6:] == (500, ME, False)


async def test_safety_skip_still_marks_but_does_not_pass_safety(tmp_path):
    with _mocks() as m:
        m.safety.return_value = SafetyResult(SafetyStatus.SKIP, "ci-blocked", "sha1")
        await auto_approve.run(_cfg(tmp_path))
    m.mark.assert_called_once()
    assert m.approve.call_args.args[4].status is SafetyStatus.SKIP


async def test_threshold_zero_never_matches(tmp_path):
    with _mocks(size=_size(viewed=0, approve=0)) as m:
        await auto_approve.run(_cfg(tmp_path, viewed=0, approve=0))
    assert m.mark.call_args.args[4] == 0  # nothing is below 0, so mark_files_viewed marks nothing
    assert m.approve.call_args.args[6] == 0
    assert m.approve.call_args.kwargs["all_viewed"] is False


async def test_empty_pr_not_approved_and_logged(tmp_path, caplog):
    with _mocks(size=_size(counts={})) as m, caplog.at_level(logging.INFO):
        await auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()
    assert "no counted files" in caplog.text
    m.restore.assert_called_once()


async def test_all_files_viewed_passes_flag_to_approve(tmp_path, caplog):
    with _mocks() as m, caplog.at_level(logging.INFO):
        m.mark.return_value = ViewedResult(ViewedStatus.DONE, all_viewed=True)
        await auto_approve.run(_cfg(tmp_path))
    assert m.approve.call_args.kwargs["all_viewed"] is True
    assert "size limit waived" in caplog.text


async def test_all_viewed_large_pr_still_goes_through_gate_and_safety(tmp_path):
    with _mocks(size=_size(counts={"a.py": 900})) as m:
        m.mark.return_value = ViewedResult(ViewedStatus.DONE, all_viewed=True)
        await auto_approve.run(_cfg(tmp_path))
    args = m.approve.call_args.args
    assert args[3].status is GateStatus.PASSED and args[4].status is SafetyStatus.SAFE
    assert args[5].total_lines == 900


async def test_all_viewed_empty_pr_is_approved(tmp_path):
    with _mocks(size=_size(counts={})) as m:
        m.mark.return_value = ViewedResult(ViewedStatus.DONE, all_viewed=True)
        await auto_approve.run(_cfg(tmp_path))
    m.approve.assert_called_once()


async def test_runner_passes_identical_writer_args_on_each_run(tmp_path):
    # Idempotency itself lives in auto_approve_github (see test_already_viewed_skipped_and_rerun_no_write
    # and test_already_approved_on_head_skips); here we only pin that the runner feeds the writers the
    # same inputs on every run, so those guards can do their job.
    with _mocks() as m:
        await auto_approve.run(_cfg(tmp_path))
        await auto_approve.run(_cfg(tmp_path))
    assert m.mark.call_count == 2 and m.approve.call_count == 2
    assert m.mark.call_args_list[0] == m.mark.call_args_list[1]
    assert m.approve.call_args_list[0] == m.approve.call_args_list[1]


@pytest.mark.parametrize("which", ["gate", "safety"])
async def test_read_failure_means_zero_writes_and_does_not_raise(tmp_path, which):
    with _mocks() as m:
        if which == "gate":
            m.gate.return_value = GateResult(GateStatus.READ_FAILED, "boom")
        else:
            m.safety.return_value = SafetyResult(SafetyStatus.READ_FAILED, "boom", "sha1")
        await auto_approve.run(_cfg(tmp_path))
    m.mark.assert_not_called()
    m.approve.assert_not_called()


async def test_viewed_read_failure_blocks_approval_and_does_not_raise(tmp_path):
    with _mocks() as m:
        m.mark.return_value = ViewedResult(ViewedStatus.READ_FAILED)
        await auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()


async def test_write_failure_does_not_raise(tmp_path):
    with _mocks() as m:
        m.approve.return_value = ApprovalResult(ApprovalStatus.WRITE_FAILED, "post failed")
        await auto_approve.run(_cfg(tmp_path))


async def test_one_pr_failing_does_not_stop_others_and_raises(tmp_path):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}, {**PR, "number": 3}]
    with _mocks(prs=prs) as m:
        m.ev.side_effect = [RuntimeError("git exploded"), _size(), _size()]
        with pytest.raises(auto_approve.AutoApproveError) as exc:
            await auto_approve.run(_cfg(tmp_path))
    assert "#1" in str(exc.value)
    assert "git exploded" in str(exc.value)
    assert [c.args[2] for c in m.approve.call_args_list] == [2, 3]
    assert m.restore.call_count == 3


def _checkout_fails_on(*numbers):
    async def checkout(pr_number, wdir, env):
        if pr_number in numbers:
            raise FatalGitError(f"git checkout of PR #{pr_number} head failed")  # noqa: TRY003
        return "sha1"

    return checkout


async def test_identified_git_failure_skips_pr_continues_and_raises(tmp_path, caplog):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}, {**PR, "number": 3}]
    with _mocks(prs=prs) as m, caplog.at_level(logging.INFO):
        m.checkout.side_effect = _checkout_fails_on(2)
        with pytest.raises(auto_approve.AutoApproveError) as exc:
            await auto_approve.run(_cfg(tmp_path))
    assert "#2" in str(exc.value)
    assert "#1" not in str(exc.value)
    assert "#3" not in str(exc.value)
    assert [c.args[2] for c in m.approve.call_args_list] == [1, 3]
    assert m.restore.call_count == 3
    assert m.clean.call_args.args == (str(tmp_path), {}, "orig")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "skipped: git failure" in r.getMessage()]
    assert [r.getMessage() for r in errors] == ["PR #2 skipped: git failure: git checkout of PR #2 head failed"]


async def test_skip_log_uses_first_stderr_line(tmp_path, caplog):
    err = subprocess.CalledProcessError(1, ["git", "checkout"], stderr=b"fatal: bad object\nsecond line\n")
    with _mocks() as m, caplog.at_level(logging.ERROR):
        m.checkout.side_effect = FatalGitError("checkout failed")
        m.checkout.side_effect.__cause__ = err
        with pytest.raises(auto_approve.AutoApproveError):
            await auto_approve.run(_cfg(tmp_path))
    messages = [r.getMessage() for r in caplog.records if "skipped: git failure" in r.getMessage()]
    assert messages == ["PR #1 skipped: git failure: fatal: bad object"]


@pytest.mark.parametrize(("cleaned", "probe"), [(False, False), (True, True), (True, None)])
async def test_unidentified_git_failure_stops_loop(tmp_path, cleaned, probe):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}, {**PR, "number": 3}]
    with _mocks(prs=prs) as m:
        m.checkout.side_effect = _checkout_fails_on(2)
        m.clean.return_value = cleaned
        m.dirty.return_value = probe
        with pytest.raises(auto_approve.AutoApproveError) as exc:
            await auto_approve.run(_cfg(tmp_path))
    assert "#2" in str(exc.value)
    assert [c.args[2] for c in m.approve.call_args_list] == [1]
    assert m.checkout.call_count == 2


async def test_fatal_git_error_stops_loop_and_skips_remaining_prs(tmp_path):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}, {**PR, "number": 3}]
    with _mocks(prs=prs) as m:
        m.ev.side_effect = [FatalGitError("tree dirty"), _size(), _size()]
        with pytest.raises(auto_approve.AutoApproveError) as exc:
            await auto_approve.run(_cfg(tmp_path))
    assert "#1" in str(exc.value)
    assert "tree dirty" in str(exc.value)
    assert "#2" not in str(exc.value)
    m.approve.assert_not_called()
    assert m.checkout.call_count == 1
    assert m.restore.call_count == 1


async def test_fatal_restore_failure_keeps_approval_logged_and_stops_loop(tmp_path, caplog):
    prs = [{**PR, "number": 1}, {**PR, "number": 2}]
    with _mocks(prs=prs) as m, caplog.at_level(logging.INFO):
        m.restore.side_effect = FatalGitError("restore failed")
        with pytest.raises(auto_approve.AutoApproveError, match="#1: restore failed"):
            await auto_approve.run(_cfg(tmp_path))
    assert [c.args[2] for c in m.approve.call_args_list] == [1]
    assert "PR #1: approval approved" in caplog.text
    assert m.checkout.call_count == 1  # PR 2 never checked out onto an unknown tree


@pytest.mark.parametrize("author", [None])
async def test_missing_author_reaches_gate_as_empty_and_blocks_writes(tmp_path, author):
    # Deleted accounts come back from the host as no author; the real gate treats an empty author as READ_FAILED.
    with _mocks(prs=[{**PR, "author": author}]) as m:
        m.gate.return_value = GateResult(GateStatus.READ_FAILED, "acting account or PR author unknown")
        await auto_approve.run(_cfg(tmp_path))
    assert m.gate.call_args.args[3] == ""
    m.mark.assert_not_called()
    m.approve.assert_not_called()


async def test_pr_list_failure_raises_with_host_message_and_cause(tmp_path):
    host = make_host()
    host.fail_next("ChangeRequests.list", CodeHostError("bad credentials"))
    with _mocks(host=host) as m, pytest.raises(auto_approve.AutoApproveError, match="bad credentials") as exc:
        await auto_approve.run(_cfg(tmp_path))
    assert isinstance(exc.value.__cause__, CodeHostError)
    m.approve.assert_not_called()
    m.checkout.assert_not_called()


async def test_pr_list_timeout_raises_auto_approve_error(tmp_path):
    host = make_host()
    host.fail_next("ChangeRequests.list", CodeHostTimeoutError("timed out", write=False))
    with _mocks(host=host) as m, pytest.raises(auto_approve.AutoApproveError, match="timed out"):
        await auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()


async def test_pr_list_unsupported_capability_stops_unwrapped(tmp_path):
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    with _mocks(host=host) as m, pytest.raises(UnsupportedCapabilityError):
        await auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()


async def test_pr_list_empty_is_not_a_failure(tmp_path, caplog):
    with _mocks(prs=[]) as m, caplog.at_level(logging.INFO):
        await auto_approve.run(_cfg(tmp_path))
    assert "no open PRs with a pending review request" in caplog.text
    listing = [c for c in m.host.calls if c.operation == "ChangeRequests.list"]
    assert len(listing) == 1 and listing[0].kwargs["requested_reviewer"] == ME


async def test_unknown_current_user_fails_the_run(tmp_path):
    host = make_host()
    host.fail_next("Session.current_user", CodeHostError("no user"))
    with _mocks(host=host) as m, pytest.raises(auto_approve.AutoApproveError, match="acting user is unknown"):
        await auto_approve.run(_cfg(tmp_path))
    m.approve.assert_not_called()


async def test_only_prs_requesting_the_acting_user_are_listed_in_number_order(tmp_path):
    host = make_host()
    _seed(host, [{"number": 7}, {"number": 3}])
    host.add_change_request(REPO, head_reference="other", number=5, author="alice", requested_reviewers=["someone"])
    with _mocks(prs=[], host=host) as m:
        await auto_approve.run(_cfg(tmp_path))
    assert [c.args[2] for c in m.approve.call_args_list] == [3, 7]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, True),
        ({"is_draft": True}, False),
        ({"state": ChangeRequestState.CLOSED}, False),
        ({"is_cross_repository": True}, False),
        ({"head_reference": ""}, False),
        ({"base_reference": ""}, False),
    ],
)
def test_is_candidate(fields, expected):
    host = make_host()
    _seed(host, [{"number": 1, **fields}])
    assert auto_approve._is_candidate(host.change_requests[REPO][0]) is expected


async def test_dry_run_passes_flag_to_writers_and_logs(tmp_path, caplog):
    with _mocks() as m, caplog.at_level(logging.INFO):
        m.approve.return_value = ApprovalResult(ApprovalStatus.DRY_RUN, "dry-run")
        await auto_approve.run(_cfg(tmp_path), dry_run=True)
    assert m.mark.call_args.kwargs["dry_run"] is True
    assert m.approve.call_args.args[8] is True
    assert "approval dry_run" in caplog.text


async def test_each_decision_is_logged(tmp_path, caplog):
    with _mocks() as m, caplog.at_level(logging.INFO):
        await auto_approve.run(_cfg(tmp_path))
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


async def test_head_moved_since_size_measured_means_no_writes(tmp_path, caplog):
    with _mocks() as m:
        m.checkout.return_value = "old-sha"  # safety reports "sha1"
        with caplog.at_level(logging.WARNING):
            await auto_approve.run(_cfg(tmp_path))
    m.mark.assert_not_called()
    m.approve.assert_not_called()
    assert "head moved" in caplog.text


async def test_fork_pr_skipped(tmp_path):
    with _mocks(prs=[{**PR, "is_cross_repository": True}]) as m:
        await auto_approve.run(_cfg(tmp_path))
    m.checkout.assert_not_called()
    m.approve.assert_not_called()


async def test_checkout_failure_cleans_working_dir_with_original_sha(tmp_path):
    with _mocks() as m:
        m.checkout.side_effect = FatalGitError("checkout failed")
        with pytest.raises(auto_approve.AutoApproveError, match="checkout failed"):
            await auto_approve.run(_cfg(tmp_path))
    m.clean.assert_called_once_with(str(tmp_path), {}, "orig")
    m.restore.assert_called_once()


async def test_successful_pr_does_not_clean(tmp_path):
    with _mocks() as m:
        await auto_approve.run(_cfg(tmp_path))
    m.clean.assert_not_called()


@pytest.mark.parametrize(("dirty", "cleaned"), [(True, True), (False, False), (None, False)])
async def test_checkout_pr_head_probes_before_checkout(tmp_path, dirty, cleaned):
    seen = []
    p = "dotharness.pr_review.runners.auto_approve."

    async def probe(cwd, env):
        seen.append("probe")
        return dirty

    async def clean(*args, **kwargs):
        seen.append(("clean", args, kwargs))
        return True

    async def run(cmd, **kwargs):
        seen.append(cmd[1])
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch("dotharness.pr_review.runners.common.git_is_dirty", probe),
        patch("dotharness.pr_review.runners.common.git_clean_working_dir", clean),
        patch(p + "run_cmd", run),
        patch(p + "get_head_sha", return_value="head"),
    ):
        assert await auto_approve._checkout_pr_head(7, str(tmp_path), {}) == "head"
    clean_calls = [c for c in seen if isinstance(c, tuple)]
    assert (len(clean_calls) == 1) is cleaned
    if cleaned:
        assert clean_calls[0][1:] == ((str(tmp_path), {}), {})
    assert seen.index("probe") < seen.index("checkout")
