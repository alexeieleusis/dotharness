import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from spec_prism_flow.build.opencode_backend import OpencodeBackend, OpencodeCommandError


class _FakeProc:
    """Stands in for one subprocess.Popen instance: communicate() replays `outcome`
    (either an (stdout, stderr, returncode) tuple or an exception to raise) on its
    first call. _kill() calls communicate() a second time to reap the process after
    a timeout-kill, mirroring the real Popen contract -- that second call just
    returns empty output rather than re-raising/re-popping."""

    def __init__(self, outcome, *, pid: int = 4242):
        self._outcome = outcome
        self._communicated = False
        self.pid = pid
        self.returncode = 0

    def communicate(self, timeout=None):
        if self._communicated:
            return b"", b""
        self._communicated = True
        if isinstance(self._outcome, Exception):
            raise self._outcome
        stdout, stderr, returncode = self._outcome
        self.returncode = returncode
        return stdout, stderr


def test_build_command_never_includes_dangerously_skip_permissions(tmp_path):
    """Regression test: --dangerously-skip-permissions is a hard failure on the
    installed opencode version -- confirmed live. This flag must never appear in a
    constructed opencode command, unlike the Claude Code backend's command."""
    cmd = OpencodeBackend._build_command(tmp_path / "instructions.md", tmp_path / "clone")

    assert "--dangerously-skip-permissions" not in cmd


def test_build_command_includes_pure_and_dir_flags_and_prompt(tmp_path):
    clone = tmp_path / "clone"
    cmd = OpencodeBackend._build_command(tmp_path / "instructions.md", clone)

    assert cmd == [
        "opencode",
        "run",
        f"Read {tmp_path / 'instructions.md'} and follow the instructions exactly.",
        "--pure",
        "--dir",
        str(clone),
    ]


def test_invoke_writes_instructions_runs_backend_and_returns_stdout(tmp_path, monkeypatch):
    written = {}

    def fake_start_process(cmd, cwd):
        instructions_path = cmd[2].removeprefix("Read ").split(" and follow", 1)[0]
        with open(instructions_path, encoding="utf-8") as f:
            written["instructions"] = f.read()
        written["cwd"] = cwd
        return _FakeProc((b"agent output\n", b"", 0))

    monkeypatch.setattr(OpencodeBackend, "_start_process", staticmethod(fake_start_process))

    backend = OpencodeBackend(timeout=123)
    result = backend.invoke("do the thing", tmp_path)

    assert result == "agent output\n"
    assert written["instructions"] == "do the thing"
    assert written["cwd"] == tmp_path


def test_invoke_cleans_up_the_temp_instructions_file(tmp_path, monkeypatch):
    seen_paths = []

    def fake_start_process(cmd, cwd):
        seen_paths.append(cmd[2].removeprefix("Read ").split(" and follow", 1)[0])
        return _FakeProc((b"", b"", 0))

    monkeypatch.setattr(OpencodeBackend, "_start_process", staticmethod(fake_start_process))

    OpencodeBackend().invoke("do the thing", tmp_path)

    assert not Path(seen_paths[0]).exists()


def test_invoke_raises_opencode_command_error_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OpencodeBackend, "_start_process", staticmethod(lambda cmd, cwd: _FakeProc((b"", b"permission denied", 3)))
    )

    with pytest.raises(OpencodeCommandError) as exc_info:
        OpencodeBackend().invoke("do the thing", tmp_path)

    assert exc_info.value.returncode == 3
    assert exc_info.value.stderr == "permission denied"
    assert exc_info.value.cmd_args[:2] == ["opencode", "run"]


def test_invoke_cleans_up_temp_file_even_when_backend_raises(tmp_path, monkeypatch):
    seen_paths = []

    def fake_start_process(cmd, cwd):
        seen_paths.append(cmd[2].removeprefix("Read ").split(" and follow", 1)[0])
        return _FakeProc((b"", b"boom", 1))

    monkeypatch.setattr(OpencodeBackend, "_start_process", staticmethod(fake_start_process))

    with pytest.raises(OpencodeCommandError):
        OpencodeBackend().invoke("do the thing", tmp_path)

    assert not Path(seen_paths[0]).exists()


def test_invoke_starts_process_in_its_own_session(tmp_path, monkeypatch):
    """Regression test for the orphaned-child-process bug: opencode must run in its
    own process group so a timeout can kill the whole group, not just the immediate
    opencode process."""
    captured_kwargs = {}

    def fake_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeProc((b"ok", b"", 0))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    OpencodeBackend().invoke("do the thing", tmp_path)

    assert captured_kwargs["start_new_session"] is True


def test_sigkill_on_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    proc = _FakeProc(subprocess.TimeoutExpired(cmd="opencode", timeout=1), pid=111)
    monkeypatch.setattr(OpencodeBackend, "_start_process", staticmethod(lambda cmd, cwd: proc))
    killpg_mock = Mock()
    monkeypatch.setattr("os.killpg", killpg_mock)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    with pytest.raises(subprocess.TimeoutExpired):
        OpencodeBackend().invoke("do the thing", tmp_path)

    killpg_mock.assert_called_once_with(111, 9)  # SIGKILL == 9, against the killed process's pid
