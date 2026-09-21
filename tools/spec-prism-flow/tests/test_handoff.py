import subprocess

import pytest

from spec_prism_flow import handoff
from spec_prism_flow.handoff import HandoffError, run_handoff


def _ok_run(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else kwargs.get("args", []), 0, b"", b"")


def test_writes_prompt_file_and_prints_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", _ok_run)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "stage_output.md").write_text("output content")

    result = run_handoff("do the thing", tmp_path, "stage")

    assert (tmp_path / "stage_prompt.md").read_text() == "do the thing"
    assert result == "output content"
    captured = capsys.readouterr()
    assert str(tmp_path / "stage_prompt.md") in captured.out
    assert str(tmp_path / "stage_output.md") in captured.out


def test_uses_explicit_output_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _ok_run)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "00-overview.md").write_text("overview content")

    result = run_handoff("prompt", tmp_path, "stage", output_filename="00-overview.md")

    assert result == "overview content"


def test_raises_handoff_error_when_output_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _ok_run)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.delenv("TMUX", raising=False)

    with pytest.raises(HandoffError) as exc_info:
        run_handoff("prompt", tmp_path, "stage")
    assert str(tmp_path / "stage_output.md") in str(exc_info.value)


def test_no_answer_reprompts_before_yes(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _ok_run)
    answers = iter([False, False, True])
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: next(answers))
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "stage_output.md").write_text("output content")

    result = run_handoff("prompt", tmp_path, "stage")

    assert result == "output content"
    assert next(answers, "exhausted") == "exhausted"


def test_clipboard_failure_is_caught_and_does_not_raise(tmp_path, monkeypatch):
    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("no pbcopy")  # noqa: TRY003

    monkeypatch.setattr(subprocess, "run", _raise_not_found)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "stage_output.md").write_text("output content")

    result = run_handoff("prompt", tmp_path, "stage")

    assert result == "output content"


def test_clipboard_nonzero_exit_is_caught_and_does_not_raise(tmp_path, monkeypatch):
    def _fail_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], 1, b"", b"boom")

    monkeypatch.setattr(subprocess, "run", _fail_run)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "stage_output.md").write_text("output content")

    result = run_handoff("prompt", tmp_path, "stage")

    assert result == "output content"


def test_clipboard_permission_error_is_caught_and_does_not_raise(tmp_path, monkeypatch):
    def _raise_permission_error(*args, **kwargs):
        raise PermissionError("pbcopy not executable")  # noqa: TRY003

    monkeypatch.setattr(subprocess, "run", _raise_permission_error)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "stage_output.md").write_text("output content")

    result = run_handoff("prompt", tmp_path, "stage")

    assert result == "output content"


def test_tmux_copy_attempted_when_tmux_env_set(tmp_path, monkeypatch):
    calls = []

    def _record_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        calls.append((cmd, kwargs.get("input")))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _record_run)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.setenv("TMUX", "fake-tmux-socket,12345,0")
    (tmp_path / "stage_output.md").write_text("output content")

    run_handoff("prompt", tmp_path, "stage")

    tmux_calls = [call for call in calls if call[0] == ["tmux", "load-buffer", "-"]]
    assert len(tmux_calls) == 1
    assert tmux_calls[0][1].decode() == str(tmp_path / "stage_prompt.md")


def test_tmux_copy_skipped_when_tmux_env_unset(tmp_path, monkeypatch):
    calls = []

    def _record_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        return subprocess.CompletedProcess(args[0] if args else [], 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _record_run)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "stage_output.md").write_text("output content")

    run_handoff("prompt", tmp_path, "stage")

    assert ["tmux", "load-buffer", "-"] not in calls


def test_tmux_failure_is_caught_and_does_not_raise(tmp_path, monkeypatch):
    def _run_side_effect(cmd, **kwargs):
        if cmd[0] == "tmux":
            raise FileNotFoundError("no tmux")  # noqa: TRY003
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _run_side_effect)
    monkeypatch.setattr(handoff.click, "confirm", lambda *a, **k: True)
    monkeypatch.setenv("TMUX", "fake-tmux-socket,12345,0")
    (tmp_path / "stage_output.md").write_text("output content")

    result = run_handoff("prompt", tmp_path, "stage")

    assert result == "output content"
