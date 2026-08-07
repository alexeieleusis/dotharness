import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from harness.runners.common import (
    build_subprocess_env,
    get_gh_token,
    git_detach_and_record,
    git_fetch_and_checkout,
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
        patch("os.killpg") as mock_kill,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        run_cmd(["sleep", "999"], cwd=str(tmp_path), env={}, timeout=1)
    mock_kill.assert_called_once()


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
    env = build_subprocess_env(["/java/bin", "/node/bin"], {}, "tok")
    assert env["PATH"].startswith("/java/bin:/node/bin:")


def test_build_env_sets_github_token():
    env = build_subprocess_env([], {}, "mytoken")
    assert env["GITHUB_TOKEN"] == "mytoken"  # noqa: S105


def test_get_gh_token_strips_whitespace():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="  tok123\n  ")
        tok = get_gh_token("echo tok123")
    assert tok == "tok123"
