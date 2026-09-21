from unittest.mock import Mock

import pytest

from spec_prism_flow.build import agent_runner
from spec_prism_flow.build.agent_runner import (
    AgentRunResult,
    branch_name,
    build_backend,
    build_prompt,
    commit_message,
    run_phase,
)
from spec_prism_flow.build.claude_backend import ClaudeBackend
from spec_prism_flow.build.opencode_backend import OpencodeBackend
from spec_prism_flow.config import AgentConfig
from spec_prism_flow.phase_file import PhaseFile


def _phase(**overrides) -> PhaseFile:
    fields = {
        "number": 7,
        "name": "build-agent-runner-leaf",
        "scope": ["spec_prism_flow/build/agent_runner.py"],
        "requirements": "Some requirements text.",
        "acceptance_criteria": ["Thing one works.", "Thing two works."],
        "manual_test_checklist": ["Run the tests."],
        "depends_on": "Phase 06 merged.",
    }
    fields.update(overrides)
    return PhaseFile(**fields)


def test_build_backend_selects_claude_backend():
    assert isinstance(build_backend(AgentConfig(backend="claude")), ClaudeBackend)


def test_build_backend_selects_opencode_backend():
    assert isinstance(build_backend(AgentConfig(backend="opencode")), OpencodeBackend)


def test_build_backend_does_not_revalidate_and_falls_through_to_claude():
    """build_backend trusts cfg.backend was already validated at config-load time
    (spec_prism_flow.config.load_config); an unrecognized value must not raise here."""
    assert isinstance(build_backend(AgentConfig(backend="not-a-real-backend")), ClaudeBackend)


def test_build_prompt_includes_scope_requirements_and_acceptance_criteria():
    phase = _phase()
    prompt = build_prompt(phase)

    assert "## Scope" in prompt
    assert "- spec_prism_flow/build/agent_runner.py" in prompt
    assert "## Requirements" in prompt
    assert "Some requirements text." in prompt
    assert "## Acceptance criteria" in prompt
    assert "- Thing one works." in prompt
    assert "- Thing two works." in prompt


def test_build_prompt_excludes_manual_test_checklist_and_depends_on():
    phase = _phase()
    prompt = build_prompt(phase)

    assert "Run the tests." not in prompt
    assert "Phase 06 merged." not in prompt


def test_branch_name_format():
    assert branch_name(_phase(number=7, name="build-agent-runner-leaf")) == "phase-07-build-agent-runner-leaf"


def test_branch_name_zero_pads_single_digit_numbers():
    assert branch_name(_phase(number=1, name="init")) == "phase-01-init"


def test_commit_message_format():
    assert commit_message(_phase(number=7, name="build-agent-runner-leaf")) == "phase 07: build-agent-runner-leaf"


def test_run_phase_checks_out_invokes_commits_and_pushes_on_success(tmp_path, monkeypatch):
    phase = _phase()
    backend = Mock()
    backend.invoke.return_value = "agent stdout"

    checkout_mock = Mock()
    commit_all_mock = Mock(return_value=True)
    push_mock = Mock()
    head_sha_mock = Mock(return_value="abc123")
    monkeypatch.setattr(agent_runner.git_ops, "checkout_fresh_branch", checkout_mock)
    monkeypatch.setattr(agent_runner.git_ops, "commit_all", commit_all_mock)
    monkeypatch.setattr(agent_runner.git_ops, "push_branch", push_mock)
    monkeypatch.setattr(agent_runner.git_ops, "head_sha", head_sha_mock)

    result = run_phase(phase, backend, tmp_path, "main")

    checkout_mock.assert_called_once_with(tmp_path, "phase-07-build-agent-runner-leaf", "main")
    backend.invoke.assert_called_once_with(build_prompt(phase), tmp_path)
    commit_all_mock.assert_called_once_with(tmp_path, "phase 07: build-agent-runner-leaf")
    push_mock.assert_called_once_with(tmp_path, "phase-07-build-agent-runner-leaf")
    head_sha_mock.assert_called_once_with(tmp_path)
    assert result == AgentRunResult(branch="phase-07-build-agent-runner-leaf", commit_sha="abc123", empty=False)


def test_run_phase_returns_empty_result_without_pushing_when_commit_all_reports_no_changes(tmp_path, monkeypatch):
    phase = _phase()
    backend = Mock()

    push_mock = Mock()
    head_sha_mock = Mock()
    monkeypatch.setattr(agent_runner.git_ops, "checkout_fresh_branch", Mock())
    monkeypatch.setattr(agent_runner.git_ops, "commit_all", Mock(return_value=False))
    monkeypatch.setattr(agent_runner.git_ops, "push_branch", push_mock)
    monkeypatch.setattr(agent_runner.git_ops, "head_sha", head_sha_mock)

    result = run_phase(phase, backend, tmp_path, "main")

    push_mock.assert_not_called()
    head_sha_mock.assert_not_called()
    assert result == AgentRunResult(branch="phase-07-build-agent-runner-leaf", commit_sha=None, empty=True)


def test_run_phase_propagates_checkout_fresh_branch_exception_and_calls_nothing_else(tmp_path, monkeypatch):
    phase = _phase()
    backend = Mock()

    monkeypatch.setattr(agent_runner.git_ops, "checkout_fresh_branch", Mock(side_effect=RuntimeError("boom")))
    commit_all_mock = Mock()
    monkeypatch.setattr(agent_runner.git_ops, "commit_all", commit_all_mock)

    with pytest.raises(RuntimeError, match="boom"):
        run_phase(phase, backend, tmp_path, "main")

    backend.invoke.assert_not_called()
    commit_all_mock.assert_not_called()


def test_run_phase_propagates_backend_invoke_exception_without_committing(tmp_path, monkeypatch):
    phase = _phase()
    backend = Mock()
    backend.invoke.side_effect = RuntimeError("agent failed")

    monkeypatch.setattr(agent_runner.git_ops, "checkout_fresh_branch", Mock())
    commit_all_mock = Mock()
    monkeypatch.setattr(agent_runner.git_ops, "commit_all", commit_all_mock)

    with pytest.raises(RuntimeError, match="agent failed"):
        run_phase(phase, backend, tmp_path, "main")

    commit_all_mock.assert_not_called()


def test_run_phase_propagates_commit_all_exception_without_pushing(tmp_path, monkeypatch):
    phase = _phase()
    backend = Mock()

    monkeypatch.setattr(agent_runner.git_ops, "checkout_fresh_branch", Mock())
    monkeypatch.setattr(agent_runner.git_ops, "commit_all", Mock(side_effect=RuntimeError("commit failed")))
    push_mock = Mock()
    monkeypatch.setattr(agent_runner.git_ops, "push_branch", push_mock)

    with pytest.raises(RuntimeError, match="commit failed"):
        run_phase(phase, backend, tmp_path, "main")

    push_mock.assert_not_called()


def test_run_phase_propagates_push_branch_exception(tmp_path, monkeypatch):
    phase = _phase()
    backend = Mock()

    monkeypatch.setattr(agent_runner.git_ops, "checkout_fresh_branch", Mock())
    monkeypatch.setattr(agent_runner.git_ops, "commit_all", Mock(return_value=True))
    monkeypatch.setattr(agent_runner.git_ops, "push_branch", Mock(side_effect=RuntimeError("push failed")))

    with pytest.raises(RuntimeError, match="push failed"):
        run_phase(phase, backend, tmp_path, "main")
