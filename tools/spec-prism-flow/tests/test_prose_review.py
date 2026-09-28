import logging
from pathlib import Path
from unittest.mock import Mock

from spec_prism_flow import prose_review
from spec_prism_flow.build import agent_runner
from spec_prism_flow.config import ProseReviewConfig


def _write_skill(repo_toplevel: Path) -> Path:
    skill_path = repo_toplevel / "skills" / "asd-ste100" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# asd-ste100 skill\n")
    return skill_path


def test_review_document_is_noop_and_makes_no_calls_when_disabled(tmp_path, monkeypatch):
    toplevel_mock = Mock()
    monkeypatch.setattr(prose_review.git_ops, "toplevel", toplevel_mock)

    prose_review.review_document(ProseReviewConfig(enabled=False), tmp_path / "requirements.md")

    toplevel_mock.assert_not_called()


def test_review_document_skips_and_warns_when_skill_file_missing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(prose_review.git_ops, "toplevel", Mock(return_value=tmp_path))
    monkeypatch.setattr(prose_review, "_skill_base", lambda: tmp_path)
    backend_mock = Mock()
    monkeypatch.setattr(agent_runner, "ClaudeBackend", Mock(return_value=backend_mock))
    target = tmp_path / "requirements.md"
    target.write_text("some content")

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    backend_mock.invoke.assert_not_called()
    assert "skill file not found" in caplog.text
    assert str(target) in caplog.text


def test_review_document_invokes_claude_backend_with_prompt_and_repo_toplevel_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(prose_review.git_ops, "toplevel", Mock(return_value=tmp_path))
    monkeypatch.setattr(prose_review, "_skill_base", lambda: tmp_path)
    skill_path = _write_skill(tmp_path)
    target = tmp_path / "workspace" / "requirements.md"
    target.parent.mkdir(parents=True)
    target.write_text("some content")

    backend_mock = Mock()
    backend_mock.invoke.return_value = "harness stdout"
    claude_backend_cls = Mock(return_value=backend_mock)
    monkeypatch.setattr(agent_runner, "ClaudeBackend", claude_backend_cls)

    prose_review.review_document(ProseReviewConfig(enabled=True, backend="claude"), target, timeout=42)

    claude_backend_cls.assert_called_once_with(timeout=42)
    backend_mock.invoke.assert_called_once()
    prompt, kwargs = backend_mock.invoke.call_args.args[0], backend_mock.invoke.call_args.kwargs
    assert kwargs["cwd"] == tmp_path
    assert str(skill_path) in prompt
    assert "workspace/requirements.md" in prompt
    assert "Strict" in prompt
    assert "in place" in prompt


def test_review_document_selects_opencode_backend_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(prose_review.git_ops, "toplevel", Mock(return_value=tmp_path))
    monkeypatch.setattr(prose_review, "_skill_base", lambda: tmp_path)
    _write_skill(tmp_path)
    target = tmp_path / "requirements.md"
    target.write_text("some content")

    backend_mock = Mock()
    opencode_backend_cls = Mock(return_value=backend_mock)
    monkeypatch.setattr(agent_runner, "OpencodeBackend", opencode_backend_cls)
    claude_backend_cls = Mock()
    monkeypatch.setattr(agent_runner, "ClaudeBackend", claude_backend_cls)

    prose_review.review_document(ProseReviewConfig(enabled=True, backend="opencode"), target)

    opencode_backend_cls.assert_called_once_with(timeout=prose_review.DEFAULT_PROSE_REVIEW_TIMEOUT_SECONDS)
    claude_backend_cls.assert_not_called()
    backend_mock.invoke.assert_called_once()


def test_review_document_swallows_exception_from_repo_toplevel_resolution(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(prose_review.git_ops, "toplevel", Mock(side_effect=RuntimeError("not a git repo")))
    target = tmp_path / "requirements.md"

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    assert "prose review of" in caplog.text
    assert "not a git repo" in caplog.text


def test_review_document_swallows_exception_when_target_outside_repo_toplevel(tmp_path, monkeypatch, caplog):
    # relative_to raises ValueError when target_path is not under the repo toplevel.
    # This is the middle of the three failure points inside review_document's
    # try/except, distinct from toplevel resolution (above) and backend invoke (below).
    target = tmp_path / "requirements.md"
    target.write_text("some content")

    # toplevel returns a directory that does NOT contain `target`
    unrelated_toplevel = tmp_path / "unrelated-repo"
    unrelated_toplevel.mkdir()
    monkeypatch.setattr(prose_review.git_ops, "toplevel", Mock(return_value=unrelated_toplevel))
    backend_mock = Mock()
    monkeypatch.setattr(agent_runner, "ClaudeBackend", Mock(return_value=backend_mock))

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    backend_mock.invoke.assert_not_called()
    assert "prose review of" in caplog.text


def test_review_document_resolves_toplevel_from_target_path_not_own_install(tmp_path, monkeypatch):
    # Regression guard: the target repo's toplevel must be derived from the target
    # document's own path, not from where spec-prism-flow itself is installed. The
    # old code anchored on this package's own `__file__`, which silently no-op'd for
    # any external target project and for every packaged (site-packages) install.
    toplevel_mock = Mock(return_value=tmp_path)
    monkeypatch.setattr(prose_review.git_ops, "toplevel", toplevel_mock)
    monkeypatch.setattr(prose_review, "_skill_base", lambda: tmp_path)
    _write_skill(tmp_path)
    target = tmp_path / "docs" / "plan.md"
    target.parent.mkdir()
    target.write_text("some content")

    backend_mock = Mock()
    monkeypatch.setattr(agent_runner, "ClaudeBackend", Mock(return_value=backend_mock))

    prose_review.review_document(ProseReviewConfig(enabled=True, backend="claude"), target)

    toplevel_mock.assert_called_once_with(target.resolve().parent)
    assert backend_mock.invoke.call_args.kwargs["cwd"] == tmp_path


def test_review_document_swallows_exception_from_backend_invoke(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(prose_review.git_ops, "toplevel", Mock(return_value=tmp_path))
    monkeypatch.setattr(prose_review, "_skill_base", lambda: tmp_path)
    _write_skill(tmp_path)
    target = tmp_path / "requirements.md"
    target.write_text("some content")

    backend_mock = Mock()
    backend_mock.invoke.side_effect = RuntimeError("backend blew up")
    monkeypatch.setattr(agent_runner, "ClaudeBackend", Mock(return_value=backend_mock))

    with caplog.at_level(logging.WARNING):
        prose_review.review_document(ProseReviewConfig(enabled=True), target)

    assert "backend blew up" in caplog.text
