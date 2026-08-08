import os
import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from harness.runners.common import (
    build_subprocess_env,
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


def test_run_cmd_timeout_kills_process_group(tmp_path):
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired([], 1)
    mock_proc.pid = os.getpid()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.getpgid", return_value=1234),
        patch("os.killpg") as mock_kill,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        run_cmd(["sleep", "999"], cwd=str(tmp_path), env={}, timeout=1)
    mock_kill.assert_called_once_with(1234, signal.SIGKILL)


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
        mock_run.return_value = MagicMock(stdout="  tok123\n  ")
        tok = get_gh_token("echo tok123")
    assert tok == "tok123"


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
