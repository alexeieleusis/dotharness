import os
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.backend import (
    Backend,
    BackendCwdDivergedError,
    OpencodePluginError,
    _CwdDivergenceMonitor,
    _lsof_cwd,
    assert_no_opencode_plugins,
)
from harness.repo_guard import RepoIdentityError


def _make_backend(tmp_xdg, backend="opencode"):
    return Backend(backend=backend, timeout=10, path_prepend=[], env_vars={})


def test_short_instructions_write_temp_file(tmp_xdg):
    b = _make_backend(tmp_xdg)
    cmd, tmp = b._build_command("Do this.")
    assert tmp is not None
    assert tmp.exists()
    assert "Do this." not in cmd
    assert str(tmp) in " ".join(cmd)


def test_long_instructions_write_temp_file(tmp_xdg):
    b = _make_backend(tmp_xdg)
    long = "x" * 5000
    cmd, tmp = b._build_command(long)
    assert tmp is not None
    assert tmp.exists()
    assert str(tmp) in " ".join(cmd)


def test_temp_file_in_xdg_tmp(tmp_xdg):
    b = _make_backend(tmp_xdg)
    _, tmp = b._build_command("Do this.")
    assert str(tmp_xdg) in str(tmp)


def test_opencode_dir_flag(tmp_xdg):
    b = _make_backend(tmp_xdg, "opencode")
    cmd, _ = b._build_command("Do this.", opencode_dir="/some/path")
    assert "--dir" in cmd
    assert "/some/path" in cmd


def test_opencode_command_shape(tmp_xdg):
    b = _make_backend(tmp_xdg, "opencode")
    cmd, _ = b._build_command("Do this.")
    assert cmd[0] == "opencode"
    assert "run" in cmd
    assert "--standalone" in cmd
    assert "--auto" in cmd
    assert "--pure" not in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_claude_command_shape(tmp_xdg):
    b = _make_backend(tmp_xdg, "claude")
    cmd, _ = b._build_command("Do this.")
    assert cmd[0] == "claude"
    assert "--dangerously-skip-permissions" in cmd


def test_nonzero_returncode_returns_completed_process(tmp_xdg, caplog):
    b = _make_backend(tmp_xdg)
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"out data", b"err data")
    mock_proc.returncode = 1
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        caplog.at_level("ERROR"),
    ):
        result = b.run("Do this.", cwd="/tmp")  # noqa: S108
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 1
    assert result.stdout == b"out data"
    assert result.stderr == b"err data"
    assert "Backend exited 1" in caplog.text


def test_temp_file_cleaned_on_success(tmp_xdg):
    b = _make_backend(tmp_xdg)
    prompt = "Do this."
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with patch("subprocess.Popen", return_value=mock_proc):
        b.run(prompt, cwd="/tmp")  # noqa: S108
    assert not list((tmp_xdg / "tmp").glob("harness_*.md"))


def test_temp_file_cleaned_on_timeout(tmp_xdg):
    b = _make_backend(tmp_xdg)
    prompt = "Do this."
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired([], 10),  # attempt 1: timed communicate raises
        (b"", b""),  # attempt 1: post-kill flush returns normally
        subprocess.TimeoutExpired([], 10),  # attempt 2: timed communicate raises
        (b"", b""),  # attempt 2: post-kill flush returns normally
    ]
    mock_proc.pid = os.getpid()
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg"),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        b.run(prompt, cwd="/tmp")  # noqa: S108
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
    prompt = "Do this."
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
        b.run(prompt, cwd="/tmp")  # noqa: S108
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


def test_temp_file_cleaned_across_retry_loop(tmp_xdg):
    b = _make_backend(tmp_xdg)
    prompt = "Do this."
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired([], 10),  # attempt 1: times out, tmp file created
        (b"", b""),  # attempt 1: post-kill flush
        (b"ok", b""),  # attempt 2: succeeds, tmp file cleaned
    ]
    mock_proc.pid = os.getpid()
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("os.killpg"),
        patch("subprocess.run", return_value=MagicMock(stdout="")),
    ):
        result = b.run(prompt, cwd="/tmp")  # noqa: S108
    assert result.stdout == b"ok"
    assert not list((tmp_xdg / "tmp").glob("harness_*.md"))


def test_no_retry_when_max_retries_zero(tmp_xdg):
    b = Backend(backend="opencode", timeout=10, path_prepend=[], env_vars={}, max_retries=0)
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired([], 10),  # timed communicate raises
        (b"", b""),  # post-kill flush returns normally
    ]
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
    env = b._build_env("/tmp")  # noqa: S108
    assert env["PATH"].startswith("/java/bin:/node/bin:")


def test_env_vars_injected(tmp_xdg):
    b = Backend("opencode", timeout=10, path_prepend=[], env_vars={"JAVA_HOME": "/java"})
    env = b._build_env("/tmp")  # noqa: S108
    assert env["JAVA_HOME"] == "/java"


def test_build_env_sets_pwd_to_cwd_regardless_of_inherited_pwd(tmp_xdg, monkeypatch):
    """Root-cause regression test: opencode v2 was confirmed (by direct reproduction)
    to trust an inherited PWD env var over its own getcwd() and re-chdir to match it.
    A wrapper script's `cd` before launching the harness left PWD stale relative to
    the cwd Backend.run() is actually given, and opencode silently operated out of
    that stale directory instead."""
    monkeypatch.setenv("PWD", "/some/stale/dir/the/wrapper/script/cd-ed/into")
    b = Backend("opencode", timeout=10, path_prepend=[], env_vars={})
    env = b._build_env("/Users/alexeieleusis/development/code_review/frontend-focused-review")
    assert env["PWD"] == "/Users/alexeieleusis/development/code_review/frontend-focused-review"  # noqa: S105


def test_invalid_backend_raises():
    with pytest.raises(ValueError, match="Unknown backend"):
        Backend("gpt4", timeout=10, path_prepend=[], env_vars={})


def test_repo_identity_not_checked_when_expected_repo_name_unset(tmp_xdg):
    b = _make_backend(tmp_xdg)
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("harness.backend.assert_repo_identity") as guard,
    ):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
    guard.assert_not_called()


def test_repo_identity_checked_before_and_after_backend_invocation(tmp_xdg):
    b = Backend(
        backend="opencode",
        timeout=10,
        path_prepend=[],
        env_vars={},
        expected_repo_name="acme/frontend",
    )
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc) as popen,
        patch("harness.backend.assert_repo_identity") as guard,
    ):
        b.run("Do this.", cwd="/some/repo")

    assert guard.call_count == 2
    for call in guard.call_args_list:
        assert call.args == (Path("/some/repo"), "acme/frontend")
    popen.assert_called_once()


def test_repo_identity_checked_before_each_retry_attempt(tmp_xdg):
    b = Backend(
        backend="opencode",
        timeout=10,
        path_prepend=[],
        env_vars={},
        expected_repo_name="acme/frontend",
    )
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
        patch("harness.backend.assert_repo_identity") as guard,
    ):
        b.run("Do this.", cwd="/some/repo")

    # pre-attempt-1, pre-attempt-2, post-attempt-2 — a regression that hoists
    # the check outside the retry loop would leave this at 1.
    assert guard.call_count == 3
    for call in guard.call_args_list:
        assert call.args == (Path("/some/repo"), "acme/frontend")


def test_repo_identity_failure_aborts_before_spawning_backend(tmp_xdg):
    b = Backend(
        backend="opencode",
        timeout=10,
        path_prepend=[],
        env_vars={},
        expected_repo_name="acme/frontend",
    )
    with (
        patch("subprocess.Popen") as popen,
        patch(
            "harness.backend.assert_repo_identity",
            side_effect=RepoIdentityError("wrong repo"),
        ),
        pytest.raises(RepoIdentityError, match="wrong repo"),
    ):
        b.run("Do this.", cwd="/some/repo")
    popen.assert_not_called()


def test_repo_identity_failure_after_backend_invocation_propagates(tmp_xdg):
    b = Backend(
        backend="opencode",
        timeout=10,
        path_prepend=[],
        env_vars={},
        expected_repo_name="acme/frontend",
    )
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch(
            "harness.backend.assert_repo_identity",
            side_effect=[None, RepoIdentityError("wrong repo")],
        ),
        pytest.raises(RepoIdentityError, match="wrong repo"),
    ):
        b.run("Do this.", cwd="/some/repo")


def test_harness_repo_not_watched_when_no_harness_repo_discovered(tmp_xdg):
    b = _make_backend(tmp_xdg)
    with patch("harness.backend._HARNESS_REPO_ROOT", None), patch("harness.backend.head_sha") as head_sha_mock:
        assert b._snapshot_harness_repo("/some/repo") is None
    head_sha_mock.assert_not_called()


def test_harness_repo_not_watched_when_cwd_is_the_harness_repo(tmp_xdg):
    b = _make_backend(tmp_xdg)
    with (
        patch("harness.backend._HARNESS_REPO_ROOT", Path("/harness/root")),
        patch("harness.backend.head_sha") as head_sha_mock,
    ):
        assert b._snapshot_harness_repo("/harness/root") is None
    head_sha_mock.assert_not_called()


def test_harness_repo_snapshotted_when_cwd_is_a_different_repo(tmp_xdg):
    b = _make_backend(tmp_xdg)
    with (
        patch("harness.backend._HARNESS_REPO_ROOT", Path("/harness/root")),
        patch("harness.backend.head_sha", return_value="abc123") as head_sha_mock,
    ):
        assert b._snapshot_harness_repo("/some/other/repo") == "abc123"
    head_sha_mock.assert_called_once_with(Path("/harness/root"))


def test_harness_repo_unchanged_check_skipped_when_no_snapshot_taken(tmp_xdg):
    b = _make_backend(tmp_xdg)
    with patch("harness.backend.assert_repo_unchanged") as guard:
        b._assert_harness_repo_unchanged(None)
    guard.assert_not_called()


def test_harness_repo_moving_during_run_raises(tmp_xdg):
    b = Backend(
        backend="opencode",
        timeout=10,
        path_prepend=[],
        env_vars={},
        expected_repo_name="acme/frontend",
    )
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with (
        patch("harness.backend._HARNESS_REPO_ROOT", Path("/harness/root")),
        patch("harness.backend.head_sha", return_value="abc123"),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("harness.backend.assert_repo_identity"),
        patch(
            "harness.backend.assert_repo_unchanged",
            side_effect=RepoIdentityError("harness repo's HEAD moved"),
        ),
        pytest.raises(RepoIdentityError, match="harness repo's HEAD moved"),
    ):
        b.run("Do this.", cwd="/some/other/repo")


def test_opencode_plugin_check_runs_before_backend_invocation(tmp_xdg):
    b = _make_backend(tmp_xdg, "opencode")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc) as popen,
        patch("harness.backend.assert_no_opencode_plugins") as guard,
    ):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
    guard.assert_called_once_with("/tmp")  # noqa: S108
    popen.assert_called_once()


def test_opencode_plugin_check_only_runs_once_per_backend_instance(tmp_xdg):
    """One Backend is built per batch (see harness/runners/*.py) and reused across
    every comment/file in it, so the plugin check should gate the batch, not each
    individual backend invocation."""
    b = _make_backend(tmp_xdg, "opencode")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("harness.backend.assert_no_opencode_plugins") as guard,
    ):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
        b.run("Do that.", cwd="/tmp")  # noqa: S108
    guard.assert_called_once_with("/tmp")  # noqa: S108


def test_opencode_plugin_check_skipped_for_claude_backend(tmp_xdg):
    b = _make_backend(tmp_xdg, "claude")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("harness.backend.assert_no_opencode_plugins") as guard,
    ):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
    guard.assert_not_called()


def test_opencode_plugin_check_failure_aborts_before_spawning_backend(tmp_xdg):
    b = _make_backend(tmp_xdg, "opencode")
    with (
        patch("subprocess.Popen") as popen,
        patch(
            "harness.backend.assert_no_opencode_plugins",
            side_effect=OpencodePluginError("a plugin is installed"),
        ),
        pytest.raises(OpencodePluginError, match="a plugin is installed"),
    ):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
    popen.assert_not_called()


def test_assert_no_opencode_plugins_passes_when_none_found():
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="No plugins found\n")):
        assert_no_opencode_plugins("/tmp")  # noqa: S108 — does not raise


def test_assert_no_opencode_plugins_scopes_check_to_backend_cwd():
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="No plugins found\n")) as run:
        assert_no_opencode_plugins("/some/repo")
    assert run.call_args.kwargs["cwd"] == "/some/repo"


def test_assert_no_opencode_plugins_raises_when_plugins_installed():
    with (
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="some-plugin@1.0.0\n")),
        pytest.raises(OpencodePluginError, match="one or more plugins installed"),
    ):
        assert_no_opencode_plugins("/tmp")  # noqa: S108


def test_assert_no_opencode_plugins_raises_when_list_command_fails():
    with (
        patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="opencode: command not found")),
        pytest.raises(OpencodePluginError, match="could not verify"),
    ):
        assert_no_opencode_plugins("/tmp")  # noqa: S108


def test_backend_run_waits_for_process_group_survivors_before_returning(tmp_xdg, monkeypatch):
    """A spawned server child can still be tearing down when the top-level backend
    process exits; run() must not return (and let the next comment start its own
    backend) until the whole process group is gone."""
    b = _make_backend(tmp_xdg, "opencode")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    mock_proc.pid = 4242

    survivors = iter(["4242 opencode serve --stdio --port 0", ""])
    monkeypatch.setattr("harness.backend.Backend._processes_in_group", staticmethod(lambda pgid: next(survivors)))
    sleeps = []
    monkeypatch.setattr("harness.backend.time.sleep", sleeps.append)

    with patch("subprocess.Popen", return_value=mock_proc):
        b.run("Do this.", cwd="/tmp")  # noqa: S108

    assert sleeps == [1]  # polled once, then found no survivors


def test_backend_run_group_wait_times_out_and_warns(tmp_xdg, monkeypatch, caplog):
    b = _make_backend(tmp_xdg, "opencode")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    mock_proc.pid = 4242

    monkeypatch.setattr("harness.backend.Backend._processes_in_group", staticmethod(lambda pgid: "4242 stuck-server"))
    monkeypatch.setattr("harness.backend.time.sleep", lambda _: None)
    # Force the deadline to already be past on the first check, so the test doesn't
    # actually wait out the real _GROUP_EXIT_WAIT_SECONDS.
    monkeypatch.setattr("harness.backend._GROUP_EXIT_WAIT_SECONDS", -1)

    with patch("subprocess.Popen", return_value=mock_proc), caplog.at_level("WARNING"):
        b.run("Do this.", cwd="/tmp")  # noqa: S108

    assert "still has member(s)" in caplog.text
    assert "stuck-server" in caplog.text


def test_opencode_run_acquires_global_lock(tmp_xdg, monkeypatch):
    b = _make_backend(tmp_xdg, "opencode")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    mock_proc.pid = 4242
    calls = []

    @contextmanager
    def fake_lock(key, *, blocking=False):
        calls.append((key, blocking))
        yield

    monkeypatch.setattr("harness.backend.acquire_lock", fake_lock)
    with patch("subprocess.Popen", return_value=mock_proc):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
    assert calls == [("opencode-global-serialize", True)]


def test_claude_run_does_not_acquire_global_lock(tmp_xdg, monkeypatch):
    b = _make_backend(tmp_xdg, "claude")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    mock_proc.pid = 4242
    calls = []

    @contextmanager
    def fake_lock(key, *, blocking=False):
        calls.append((key, blocking))
        yield

    monkeypatch.setattr("harness.backend.acquire_lock", fake_lock)
    with patch("subprocess.Popen", return_value=mock_proc):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
    assert calls == []


def test_lsof_cwd_parses_n_line():
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="p4242\nfcwd\nn/some/dir\n")):
        assert _lsof_cwd(4242) == "/some/dir"


def test_lsof_cwd_returns_none_when_process_gone():
    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        assert _lsof_cwd(4242) is None


def test_cwd_divergence_monitor_kills_process_on_mismatch(monkeypatch):
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None  # still running
    monkeypatch.setattr("harness.backend._lsof_cwd", lambda pid: "/wrong/dir")
    monkeypatch.setattr("harness.backend.os.getpgid", lambda pid: pid)
    killed = {}
    monkeypatch.setattr("harness.backend.os.killpg", lambda pgid, sig: killed.update(pgid=pgid, sig=sig))

    monitor = _CwdDivergenceMonitor(proc, "/expected/dir", "prefix: ")
    monitor._stop_event.wait = lambda timeout: False
    monitor.run()

    assert monitor.diverged_to == "/wrong/dir"
    assert killed == {"pgid": 4242, "sig": signal.SIGKILL}


def test_cwd_divergence_monitor_stops_once_process_exits(monkeypatch):
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = 0  # already exited

    monitor = _CwdDivergenceMonitor(proc, "/expected/dir", "")
    monitor._stop_event.wait = lambda timeout: False
    monitor.run()

    assert monitor.diverged_to is None


def test_backend_run_raises_and_does_not_trust_output_on_cwd_divergence(tmp_xdg, monkeypatch):
    b = _make_backend(tmp_xdg, "opencode")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"looks fine", b"")
    mock_proc.returncode = 0
    mock_proc.pid = 4242

    class _FakeMonitor:
        def __init__(self, proc, expected_cwd, prefix):
            self.diverged_to = "/some/other/repo"

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("harness.backend._CwdDivergenceMonitor", _FakeMonitor)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        pytest.raises(BackendCwdDivergedError, match="diverged"),
    ):
        b.run("Do this.", cwd="/tmp")  # noqa: S108
