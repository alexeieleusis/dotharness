import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from harness.backend import INLINE_THRESHOLD_BYTES, Backend


def _make_backend(tmp_xdg, backend="opencode"):
    return Backend(backend=backend, timeout=10, path_prepend=[], env_vars={})


def test_short_instructions_inline(tmp_xdg):
    b = _make_backend(tmp_xdg)
    cmd, tmp = b._build_command("Do this.")
    assert tmp is None
    assert "Do this." in cmd


def test_long_instructions_write_temp_file(tmp_xdg):
    b = _make_backend(tmp_xdg)
    long = "x" * (INLINE_THRESHOLD_BYTES + 1)
    cmd, tmp = b._build_command(long)
    assert tmp is not None
    assert tmp.exists()
    assert str(tmp) in " ".join(cmd)


def test_temp_file_in_xdg_tmp(tmp_xdg):
    b = _make_backend(tmp_xdg)
    long = "x" * (INLINE_THRESHOLD_BYTES + 1)
    _, tmp = b._build_command(long)
    assert str(tmp_xdg) in str(tmp)


def test_opencode_command_shape(tmp_xdg):
    b = _make_backend(tmp_xdg, "opencode")
    cmd, _ = b._build_command("Do this.")
    assert cmd[0] == "opencode"
    assert "run" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--pure" in cmd


def test_claude_command_shape(tmp_xdg):
    b = _make_backend(tmp_xdg, "claude")
    cmd, _ = b._build_command("Do this.")
    assert cmd[0] == "claude"
    assert "--dangerously-skip-permissions" in cmd


def test_temp_file_cleaned_on_success(tmp_xdg):
    b = _make_backend(tmp_xdg)
    long = "x" * (INLINE_THRESHOLD_BYTES + 1)
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with patch("subprocess.Popen", return_value=mock_proc):
        b.run(long, cwd="/tmp")  # noqa: S108
    assert not list((tmp_xdg / "tmp").glob("harness_*.md"))


def test_temp_file_cleaned_on_timeout(tmp_xdg):
    b = _make_backend(tmp_xdg)
    long = "x" * (INLINE_THRESHOLD_BYTES + 1)
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired([], 10)
    mock_proc.pid = os.getpid()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg"),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        b.run(long, cwd="/tmp")  # noqa: S108
    assert not list((tmp_xdg / "tmp").glob("harness_*.md"))


@pytest.mark.parametrize(
    "survivor_stdout, expect_warning",
    [
        ("12345 opencode run --dangerously-skip-permissions ...\n", True),
        ("", False),
    ],
)
def test_timeout_kill_warns_iff_backend_survives(tmp_xdg, caplog, survivor_stdout, expect_warning):
    b = _make_backend(tmp_xdg)
    long = "x" * (INLINE_THRESHOLD_BYTES + 1)
    mock_proc = MagicMock()
    # Default max_retries=1 means two attempts; both time out here so the final
    # TimeoutExpired still propagates, but the survival check runs on each attempt.
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired([], 10),
        (b"", b""),
        subprocess.TimeoutExpired([], 10),
        (b"", b""),
    ]
    mock_proc.pid = os.getpid()
    pgrep_result = MagicMock(stdout=survivor_stdout)
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg"),
        patch("subprocess.run", return_value=pgrep_result),
        pytest.raises(subprocess.TimeoutExpired),
        caplog.at_level("WARNING"),
    ):
        b.run(long, cwd="/tmp")  # noqa: S108
    assert ("still alive afterward" in caplog.text) == expect_warning


def test_retries_once_on_timeout_then_succeeds(tmp_xdg):
    b = _make_backend(tmp_xdg)
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired([], 10),  # attempt 1: times out
        (b"", b""),  # attempt 1: post-kill flush
        (b"ok", b""),  # attempt 2: succeeds
    ]
    mock_proc.pid = os.getpid()
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg"),
        patch("subprocess.run", return_value=MagicMock(stdout="")),
    ):
        result = b.run("Do this.", cwd="/tmp")  # noqa: S108
    assert result.stdout == b"ok"
    assert mock_proc.communicate.call_count == 3


def test_no_retry_when_max_retries_zero(tmp_xdg):
    b = Backend(backend="opencode", timeout=10, path_prepend=[], env_vars={}, max_retries=0)
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired([], 10)
    mock_proc.pid = os.getpid()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg"),
        patch("subprocess.run", return_value=MagicMock(stdout="")),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
    assert mock_proc.communicate.call_count == 2


def test_path_prepend_in_env(tmp_xdg):
    b = Backend("opencode", timeout=10, path_prepend=["/java/bin", "/node/bin"], env_vars={})
    env = b._build_env()
    assert env["PATH"].startswith("/java/bin:/node/bin:")


def test_env_vars_injected(tmp_xdg):
    b = Backend("opencode", timeout=10, path_prepend=[], env_vars={"JAVA_HOME": "/java"})
    env = b._build_env()
    assert env["JAVA_HOME"] == "/java"


def test_invalid_backend_raises():
    with pytest.raises(ValueError, match="Unknown backend"):
        Backend("gpt4", timeout=10, path_prepend=[], env_vars={})
