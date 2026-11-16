import io
import subprocess

import pytest

from dotharness.spec_prism_flow import handoff
from dotharness.spec_prism_flow.handoff import HandoffError, run_handoff


def _confirm_and_write(output_path, content):
    def _confirm(*args, **kwargs):
        output_path.write_text(content)
        return True

    return _confirm


def test_writes_prompt_file_and_prints_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(handoff.typer, "confirm", _confirm_and_write(tmp_path / "stage_output.md", "output content"))

    result = run_handoff("do the thing", tmp_path, "stage")

    assert (tmp_path / "stage_prompt.md").read_text() == "do the thing"
    assert result == "output content"
    captured = capsys.readouterr()
    assert str(tmp_path / "stage_prompt.md") in captured.out
    assert str(tmp_path / "stage_output.md") in captured.out


def test_uses_explicit_output_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(handoff.typer, "confirm", _confirm_and_write(tmp_path / "00-overview.md", "overview content"))

    result = run_handoff("prompt", tmp_path, "stage", output_filename="00-overview.md")

    assert result == "overview content"


def test_raises_handoff_error_when_output_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(handoff.typer, "confirm", lambda *a, **k: True)

    with pytest.raises(HandoffError) as exc_info:
        run_handoff("prompt", tmp_path, "stage")
    assert str(tmp_path / "stage_output.md") in str(exc_info.value)


def test_stale_output_from_prior_attempt_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(handoff.typer, "confirm", lambda *a, **k: True)
    (tmp_path / "stage_output.md").write_text("stale content from a prior run")

    with pytest.raises(HandoffError) as exc_info:
        run_handoff("prompt", tmp_path, "stage")
    assert str(tmp_path / "stage_output.md") in str(exc_info.value)


def test_no_answer_reprompts_before_yes(tmp_path, monkeypatch):
    answers = iter([False, False, True])
    output_path = tmp_path / "stage_output.md"

    def _confirm(*args, **kwargs):
        answer = next(answers)
        if answer:
            output_path.write_text("output content")
        return answer

    monkeypatch.setattr(handoff.typer, "confirm", _confirm)

    result = run_handoff("prompt", tmp_path, "stage")

    assert result == "output content"
    assert next(answers, "exhausted") == "exhausted"


def test_does_not_spawn_any_subprocess(tmp_path, monkeypatch):
    def _forbidden_run(*args, **kwargs):
        raise AssertionError

    monkeypatch.setattr(subprocess, "run", _forbidden_run)
    monkeypatch.setattr(handoff.typer, "confirm", _confirm_and_write(tmp_path / "stage_output.md", "output content"))
    monkeypatch.setenv("TMUX", "fake-tmux-socket,12345,0")

    result = run_handoff("prompt", tmp_path, "stage")

    assert result == "output content"


def test_handoff_contract_strings_with_real_confirm(tmp_path, monkeypatch, capsys):
    output_path = tmp_path / "stage_output.md"
    monkeypatch.setattr("sys.stdin", io.StringIO("n\ny\n"))

    real_confirm = handoff.typer.confirm

    def _confirm(*args, **kwargs):
        result = real_confirm(*args, **kwargs)
        if result:
            output_path.write_text("output content")
        return result

    monkeypatch.setattr(handoff.typer, "confirm", _confirm)

    run_handoff("prompt", tmp_path, "stage")

    out = capsys.readouterr().out
    assert f"Prompt written to: {tmp_path / 'stage_prompt.md'}" in out
    assert f"Expected output at: {output_path}" in out
    assert out.count("Agent finished writing output? [y/N]") == 2
