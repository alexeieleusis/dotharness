from pathlib import Path
from unittest.mock import Mock

import pytest

from spec_prism_flow.build.opencode_backend import OpencodeBackend, OpencodeCommandError


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

    def fake_run(cmd, **kwargs):
        instructions_path = cmd[2].removeprefix("Read ").split(" and follow", 1)[0]
        with open(instructions_path, encoding="utf-8") as f:
            written["instructions"] = f.read()
        written["cwd"] = kwargs["cwd"]
        written["timeout"] = kwargs["timeout"]
        return Mock(returncode=0, stdout="agent output\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    backend = OpencodeBackend(timeout=123)
    result = backend.invoke("do the thing", tmp_path)

    assert result == "agent output\n"
    assert written["instructions"] == "do the thing"
    assert written["cwd"] == tmp_path
    assert written["timeout"] == 123


def test_invoke_cleans_up_the_temp_instructions_file(tmp_path, monkeypatch):
    seen_paths = []

    def fake_run(cmd, **kwargs):
        seen_paths.append(cmd[2].removeprefix("Read ").split(" and follow", 1)[0])
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    OpencodeBackend().invoke("do the thing", tmp_path)

    assert not Path(seen_paths[0]).exists()


def test_invoke_raises_opencode_command_error_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **k: Mock(returncode=3, stdout="", stderr="permission denied"))

    with pytest.raises(OpencodeCommandError) as exc_info:
        OpencodeBackend().invoke("do the thing", tmp_path)

    assert exc_info.value.returncode == 3
    assert exc_info.value.stderr == "permission denied"
    assert exc_info.value.cmd_args[:2] == ["opencode", "run"]


def test_invoke_cleans_up_temp_file_even_when_backend_raises(tmp_path, monkeypatch):
    seen_paths = []

    def fake_run(cmd, **kwargs):
        seen_paths.append(cmd[2].removeprefix("Read ").split(" and follow", 1)[0])
        return Mock(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(OpencodeCommandError):
        OpencodeBackend().invoke("do the thing", tmp_path)

    assert not Path(seen_paths[0]).exists()
