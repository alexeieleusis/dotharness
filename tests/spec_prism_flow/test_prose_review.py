import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from dotharness.ai_backend import ClaudeOptions, OpencodeOptions
from dotharness.git_utils import GitError
from dotharness.spec_prism_flow import prose_review
from dotharness.spec_prism_flow.config import ProseReviewConfig
from dotharness.subprocess_runner import RunResult

OK = RunResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def launch(monkeypatch):
    mock = Mock(return_value=OK)
    monkeypatch.setattr(prose_review, "launch_sync", mock)
    return mock


def _setup(tmp_path, monkeypatch, rel="requirements.md"):
    monkeypatch.setattr(prose_review, "git_toplevel", Mock(return_value=tmp_path))
    skill = tmp_path / "SKILL.md"
    skill.write_text("# skill\n")
    monkeypatch.setattr(prose_review, "_skill_path", lambda: skill)
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("some content")
    return target, skill


def test_noop_when_disabled(tmp_path, monkeypatch, launch):
    toplevel_mock = Mock()
    monkeypatch.setattr(prose_review, "git_toplevel", toplevel_mock)

    prose_review.review_document(ProseReviewConfig(enabled=False), tmp_path / "requirements.md")

    toplevel_mock.assert_not_called()
    launch.assert_not_called()


def test_skill_path_resolves_via_importlib_resources():
    path = prose_review._skill_path()

    assert path.is_file()
    assert path.parts[-3:] == ("skills", "asd-ste100", "SKILL.md")
    assert "dotharness" in path.parts


def test_skips_and_warns_when_skill_file_missing(tmp_path, monkeypatch, caplog, launch):
    target, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(prose_review, "_skill_path", lambda: tmp_path / "nope.md")

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    launch.assert_not_called()
    assert "skill file not found" in caplog.text
    assert str(target) in caplog.text


def test_claude_call_shape(tmp_path, monkeypatch, launch):
    target, skill = _setup(tmp_path, monkeypatch, "workspace/requirements.md")

    prose_review.review_document(ProseReviewConfig(enabled=True, backend="claude"), target)

    launch.assert_called_once()
    args, kwargs = launch.call_args
    assert args[0] == "claude"
    prompt = args[1]
    assert str(skill) in prompt
    assert "workspace/requirements.md" in prompt
    assert "Strict" in prompt
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 900
    assert kwargs["max_retries"] == 0
    assert kwargs["backend_options"] == ClaudeOptions(skip_permissions=True, disable_slash_commands=True)


def test_opencode_call_shape(tmp_path, monkeypatch, launch):
    target, _ = _setup(tmp_path, monkeypatch)

    prose_review.review_document(ProseReviewConfig(enabled=True, backend="opencode"), target, timeout=42)

    args, kwargs = launch.call_args
    assert args[0] == "opencode"
    assert kwargs["timeout"] == 42
    assert kwargs["max_retries"] == 0
    assert kwargs["backend_options"] == OpencodeOptions(standalone=True, opencode_dir=tmp_path)
    assert kwargs["backend_options"].auto is False


def test_toplevel_derived_from_target_path(tmp_path, monkeypatch, launch):
    target, _ = _setup(tmp_path, monkeypatch, "docs/plan.md")
    toplevel_mock = Mock(return_value=tmp_path)
    monkeypatch.setattr(prose_review, "git_toplevel", toplevel_mock)

    prose_review.review_document(ProseReviewConfig(enabled=True), target)

    toplevel_mock.assert_called_once_with(target.resolve().parent)


def test_swallows_git_error(tmp_path, monkeypatch, caplog, launch):
    monkeypatch.setattr(prose_review, "git_toplevel", Mock(side_effect=GitError("not a git repo")))

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), tmp_path / "requirements.md")

    assert "prose review of" in caplog.text
    assert "not a git repo" in caplog.text
    launch.assert_not_called()


def test_swallows_target_outside_toplevel(tmp_path, monkeypatch, caplog, launch):
    target, _ = _setup(tmp_path, monkeypatch)
    other = tmp_path / "unrelated"
    other.mkdir()
    monkeypatch.setattr(prose_review, "git_toplevel", Mock(return_value=other))

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    launch.assert_not_called()
    assert "prose review of" in caplog.text


def test_swallows_launch_exception(tmp_path, monkeypatch, caplog, launch):
    target, _ = _setup(tmp_path, monkeypatch)
    launch.side_effect = RuntimeError("backend blew up")

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    assert "prose review of" in caplog.text
    assert "backend blew up" in caplog.text


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (RunResult(returncode=124, stdout="", stderr="", timed_out=True), "timed out"),
        (RunResult(returncode=127, stdout="", stderr="", not_found=True), "not found"),
        (RunResult(returncode=3, stdout="", stderr=""), "exited with code 3"),
    ],
)
def test_warns_on_bad_result_without_leaking_secrets(tmp_path, monkeypatch, caplog, launch, result, expected):
    target, skill = _setup(tmp_path, monkeypatch)
    secret = RunResult(
        returncode=result.returncode,
        stdout="SECRET-STDOUT",
        stderr="SECRET-STDERR argv env",
        timed_out=result.timed_out,
        not_found=result.not_found,
    )
    launch.return_value = secret

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    assert expected in caplog.text
    prompt = launch.call_args.args[1]
    assert "SECRET" not in caplog.text
    assert "Strict mode" not in caplog.text
    assert prompt not in caplog.text
    assert "env" not in caplog.text.replace("environment", "")
    assert Path(skill).name not in caplog.text


def test_success_logs_nothing(tmp_path, monkeypatch, caplog, launch):
    target, _ = _setup(tmp_path, monkeypatch)

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    assert caplog.text == ""
