import os
import signal
import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from harness.runners.common import (
    FatalGitError,
    add_reviewer,
    build_subprocess_env,
    get_current_user,
    get_gh_token,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    run_cmd,
)


def test_run_cmd_success(tmp_path):
    result = run_cmd(["echo", "hello"], cwd=str(tmp_path), env=os.environ.copy(), timeout=5)
    assert result.returncode == 0


def test_run_cmd_nonzero_raises(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        run_cmd(["false"], cwd=str(tmp_path), env=os.environ.copy(), timeout=5)


@contextmanager
def _patched_timeout_kill(communicate_side_effect):
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = communicate_side_effect
    mock_proc.pid = os.getpid()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.getpgid", return_value=1234),
        patch("os.killpg") as mock_kill,
    ):
        yield mock_kill


def test_run_cmd_timeout_sends_sigterm_first(tmp_path):
    with (
        _patched_timeout_kill([subprocess.TimeoutExpired([], 1), (b"", b"")]) as mock_kill,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        run_cmd(["sleep", "999"], cwd=str(tmp_path), env={}, timeout=1)
    mock_kill.assert_called_once_with(1234, signal.SIGTERM)


def test_run_cmd_timeout_sigkills_if_still_alive_after_grace_period(tmp_path):
    side_effect = [subprocess.TimeoutExpired([], 1), subprocess.TimeoutExpired([], 10), (b"", b"")]
    with (
        _patched_timeout_kill(side_effect) as mock_kill,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        run_cmd(["sleep", "999"], cwd=str(tmp_path), env={}, timeout=1)
    assert mock_kill.call_args_list == [
        ((1234, signal.SIGTERM),),
        ((1234, signal.SIGKILL),),
    ]


def test_git_detach_records_sha(tmp_path):
    sha = "abc1234def5678"
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(stdout=f"{sha}\n".encode())
        result = git_detach_and_record(str(tmp_path), {})
    assert result == sha


def test_git_fetch_and_checkout_calls(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        git_fetch_and_checkout("feature-branch", str(tmp_path), {})
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("fetch" in c for c in calls)
    assert any("checkout" in c and "feature-branch" in c for c in calls)


def test_git_detach_raises_fatal_error_on_called_process_error(tmp_path):
    with (
        patch("harness.runners.common.run_cmd", side_effect=subprocess.CalledProcessError(1, ["git", "checkout"])),
        pytest.raises(FatalGitError),
    ):
        git_detach_and_record(str(tmp_path), {})


def test_git_fetch_and_checkout_raises_fatal_error_on_called_process_error(tmp_path):
    with (
        patch("harness.runners.common.run_cmd", side_effect=subprocess.CalledProcessError(1, ["git", "fetch"])),
        pytest.raises(FatalGitError),
    ):
        git_fetch_and_checkout("feature-branch", str(tmp_path), {})


def test_build_env_prepends_path():
    original_path = os.environ.get("PATH", "")
    env = build_subprocess_env(["/java/bin", "/node/bin"], {}, "tok")
    assert env["PATH"].startswith("/java/bin:/node/bin:")
    assert original_path in env["PATH"]


def test_build_env_sets_github_token():
    env = build_subprocess_env([], {}, "mytoken")
    assert env["GITHUB_TOKEN"] == "mytoken"  # noqa: S105


def test_get_gh_token_strips_whitespace():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="  tok123\n  ")
        tok = get_gh_token("echo tok123")
    assert tok == "tok123"


def test_get_gh_token_nonzero_exit_raises_even_with_partial_stdout():
    # A nonzero exit means gh_token_cmd itself failed — trusting stray stdout here
    # (e.g. a warning banner some wrapper printed before failing) would silently hand
    # back a bogus "token" instead of surfacing the failure.
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="partial-token", stderr="auth error")
        with pytest.raises(RuntimeError, match="auth error"):
            get_gh_token("fail_cmd")


def test_get_gh_token_nonzero_exit_empty_stdout_raises():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth failed")
        with pytest.raises(RuntimeError, match="auth failed"):
            get_gh_token("fail_cmd")


def test_get_gh_token_timeout_raises():
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("gh_token_cmd", 10)
        with pytest.raises(subprocess.TimeoutExpired):
            get_gh_token("gh_token_cmd")


def test_get_gh_token_stderr_only_raises():
    # Exit 0 but blank stdout: build_subprocess_env would treat this token as falsy
    # and silently skip setting GITHUB_TOKEN, so this must raise rather than return ""
    # — an unset GITHUB_TOKEN makes every subsequent `gh` call fall back to whichever
    # account `gh auth status` currently has active, not the one gh_token_cmd names.
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="token not configured")
        with pytest.raises(RuntimeError, match="token not configured"):
            get_gh_token("echo")


def test_add_reviewer_logs_error_on_failure(caplog):
    # preserve_reviewer_request calls add_reviewer from a `finally` with nothing else
    # watching the outcome — a failed re-add must be logged, or it's invisible.
    with (
        patch(
            "harness.runners.common.run_cmd",
            return_value=MagicMock(returncode=1, stderr=b"could not add reviewer: not found"),
        ),
        caplog.at_level("ERROR"),
    ):
        add_reviewer(9, "acme/frontend", "alice", {})
    assert any("could not add reviewer" in r.message for r in caplog.records)


def test_add_reviewer_silent_on_success(caplog):
    with (
        patch("harness.runners.common.run_cmd", return_value=MagicMock(returncode=0, stderr=b"")),
        caplog.at_level("ERROR"),
    ):
        add_reviewer(9, "acme/frontend", "alice", {})
    assert not caplog.records


def test_get_current_user_returns_login_on_success():
    with patch("harness.runners.common.run_cmd", return_value=MagicMock(returncode=0, stdout=b"alice\n", stderr=b"")):
        assert get_current_user({}) == "alice"


def test_get_current_user_logs_and_returns_blank_on_failure(caplog):
    # Fails open (returns "" rather than raising) since every caller already treats a
    # blank current_user as a safe no-op — but the failure must still be logged.
    with (
        patch(
            "harness.runners.common.run_cmd",
            return_value=MagicMock(returncode=1, stdout=b"", stderr=b"gh: token revoked"),
        ),
        caplog.at_level("ERROR"),
    ):
        assert get_current_user({}) == ""
    assert any("token revoked" in r.message for r in caplog.records)


def _git_restore_side_effect(is_ancestor_returncode):
    def side_effect(cmd, **kwargs):
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"deadbeef1234\n", stderr=b"")
        if "merge-base" in cmd:
            return MagicMock(returncode=is_ancestor_returncode, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    return side_effect


def test_git_restore_preserves_commits_not_on_origin(tmp_path, caplog):
    mock_run = MagicMock(side_effect=_git_restore_side_effect(is_ancestor_returncode=1))
    with patch("harness.runners.common.run_cmd", mock_run), caplog.at_level("WARNING"):
        git_restore("orig-sha", "my-branch", str(tmp_path), {})
    update_ref_calls = [c for c in mock_run.call_args_list if c.args and "update-ref" in c.args[0]]
    assert len(update_ref_calls) == 1
    assert update_ref_calls[0].args[0] == [
        "git",
        "update-ref",
        "refs/harness-recovery/my-branch-deadbeef1234",
        "deadbeef1234",
    ]
    # the local branch must still get force-checked-out-away-from and deleted afterward
    checkout_calls = [c for c in mock_run.call_args_list if c.args and "checkout" in c.args[0]]
    branch_delete_calls = [c for c in mock_run.call_args_list if c.args and c.args[0][:3] == ["git", "branch", "-D"]]
    assert checkout_calls and branch_delete_calls
    assert any("preserved it at" in r.message for r in caplog.records)


def test_git_restore_skips_recovery_ref_when_branch_fully_pushed(tmp_path, caplog):
    mock_run = MagicMock(side_effect=_git_restore_side_effect(is_ancestor_returncode=0))
    with patch("harness.runners.common.run_cmd", mock_run), caplog.at_level("WARNING"):
        git_restore("orig-sha", "my-branch", str(tmp_path), {})
    update_ref_calls = [c for c in mock_run.call_args_list if c.args and "update-ref" in c.args[0]]
    assert update_ref_calls == []
    assert not any("preserved it at" in r.message for r in caplog.records)


def test_git_restore_skips_preserve_check_when_branch_empty(tmp_path):
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=b"", stderr=b""))
    with patch("harness.runners.common.run_cmd", mock_run):
        git_restore("orig-sha", "", str(tmp_path), {})
    # no branch name means nothing to preserve or delete: only the checkout happens
    assert mock_run.call_count == 1
    assert mock_run.call_args_list[0].args[0][:2] == ["git", "checkout"]


def test_git_restore_logs_error_when_recovery_ref_cannot_be_saved(tmp_path, caplog):
    def side_effect(cmd, **kwargs):
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"deadbeef1234\n", stderr=b"")
        if "merge-base" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        if "update-ref" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"failed")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("harness.runners.common.run_cmd", side_effect=side_effect), caplog.at_level("ERROR"):
        git_restore("orig-sha", "my-branch", str(tmp_path), {})
    assert any("could NOT be preserved" in r.message for r in caplog.records)
