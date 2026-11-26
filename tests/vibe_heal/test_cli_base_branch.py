"""Base branch resolution for the cleanup-family commands: cleanup, cleanup-pr, dedupe-branch."""

from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from dotharness.code_host import CodeHostError, RepositoryRef
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.vibe_heal.ai_tools.base import AIToolType
from dotharness.vibe_heal.cleanup.orchestrator import CleanupResult
from dotharness.vibe_heal.cleanup_pr.orchestrator import CleanupPrResult
from dotharness.vibe_heal.cli import app
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.orchestrator import DedupeBranchResult

runner = CliRunner()

# command, orchestrator class patched in cli, orchestrator method, successful result
COMMANDS = [
    ("cleanup", "CleanupOrchestrator", "cleanup_branch", CleanupResult(success=True, files_processed=[])),
    ("cleanup-pr", "CleanupPrOrchestrator", "cleanup_pr", CleanupPrResult(success=True, files_processed=[])),
    (
        "dedupe-branch",
        "DedupeBranchOrchestrator",
        "dedupe_branch",
        DedupeBranchResult(success=True, files_processed=[]),
    ),
]
command_param = pytest.mark.parametrize(("command", "orchestrator_name", "method", "result"), COMMANDS)


@dataclass
class Env:
    config: MagicMock
    orchestrator: MagicMock
    host: InMemoryCodeHost


@pytest.fixture
def env(fake_code_host: InMemoryCodeHost) -> Iterator[Env]:
    with (
        patch("dotharness.vibe_heal.cli.SonarQubeClient") as client_class,
        patch("dotharness.vibe_heal.cli.AIToolFactory") as ai_factory,
        patch("dotharness.vibe_heal.cli.VibeHealConfig") as config_class,
        patch("dotharness.vibe_heal.cli.BranchAnalyzer") as analyzer_class,
        patch("dotharness.vibe_heal.cli.CleanupOrchestrator") as cleanup_class,
        patch("dotharness.vibe_heal.cli.CleanupPrOrchestrator") as cleanup_pr_class,
        patch("dotharness.vibe_heal.cli.DedupeBranchOrchestrator") as dedupe_class,
    ):
        config = MagicMock(spec=VibeHealConfig)
        config.ai_tool = None
        config.base_branch = None
        config_class.return_value = config
        ai_tool = MagicMock()
        ai_tool.is_available.return_value = True
        ai_factory.detect_available.return_value = AIToolType.CLAUDE_CODE
        ai_factory.create.return_value = ai_tool
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client
        analyzer_class.return_value.validate_branch_exists.return_value = True

        orchestrator = MagicMock()
        for _, _name, method, result in COMMANDS:
            setattr(orchestrator, method, AsyncMock(return_value=result))
        for orchestrator_class in (cleanup_class, cleanup_pr_class, dedupe_class):
            orchestrator_class.return_value = orchestrator
        yield Env(config, orchestrator, fake_code_host)


def _passed_base(env: Env, method: str) -> str:
    getattr(env.orchestrator, method).assert_called_once()
    return getattr(env.orchestrator, method).call_args.kwargs["base_branch"]


@command_param
def test_flag_wins_over_setting_and_host(env: Env, command: str, orchestrator_name: str, method: str, result: object):
    env.config.base_branch = "release"
    env.host.set_default_branch("develop")
    out = runner.invoke(app, [command, "--base-branch", "upstream/custom"])
    assert out.exit_code == 0
    assert _passed_base(env, method) == "upstream/custom"


@command_param
def test_short_flag_is_accepted(env: Env, command: str, orchestrator_name: str, method: str, result: object):
    out = runner.invoke(app, [command, "-b", "origin/x"])
    assert out.exit_code == 0
    assert _passed_base(env, method) == "origin/x"


@command_param
def test_setting_used_without_flag_and_host_not_read(
    env: Env, monkeypatch: pytest.MonkeyPatch, command: str, orchestrator_name: str, method: str, result: object
):
    env.config.base_branch = "release"
    env.host.set_default_branch("develop")
    reads = AsyncMock(side_effect=AssertionError("host must not be read"))
    monkeypatch.setattr(type(env.host.code_host().repository), "default_branch", reads, raising=False)
    out = runner.invoke(app, [command])
    assert out.exit_code == 0
    assert _passed_base(env, method) == "origin/release"
    reads.assert_not_called()


@command_param
def test_host_default_used_when_neither(env: Env, command: str, orchestrator_name: str, method: str, result: object):
    env.host.set_default_branch("develop")
    out = runner.invoke(app, [command])
    assert out.exit_code == 0
    assert _passed_base(env, method) == "origin/develop"


@command_param
def test_main_default_repo_is_unchanged(env: Env, command: str, orchestrator_name: str, method: str, result: object):
    out = runner.invoke(app, [command])
    assert out.exit_code == 0
    assert _passed_base(env, method) == "origin/main"


@pytest.mark.parametrize("error", [CodeHostError("boom"), OSError("gh missing")], ids=["CodeHostError", "OSError"])
@command_param
def test_host_failure_falls_back_with_warning(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    command: str,
    orchestrator_name: str,
    method: str,
    result: object,
    caplog: pytest.LogCaptureFixture,
):
    host = env.host.code_host()
    monkeypatch.setattr(host.repository, "default_branch", AsyncMock(side_effect=error))
    monkeypatch.setattr("dotharness.vibe_heal.cli._start_code_host", lambda _config: host)
    with caplog.at_level("DEBUG"):
        out = runner.invoke(app, [command])
    assert out.exit_code == 0
    assert _passed_base(env, method) == "origin/main"
    assert "could not read the host default branch" not in caplog.text.lower()
    assert "could not determine the default branch" in out.output.lower()


@command_param
def test_host_creation_failure_falls_back_with_warning(
    env: Env, monkeypatch: pytest.MonkeyPatch, command: str, orchestrator_name: str, method: str, result: object
):
    def broken(_config: object) -> None:
        raise CodeHostError("cannot create")  # noqa: TRY003

    monkeypatch.setattr("dotharness.vibe_heal.cli._start_code_host", broken)
    out = runner.invoke(app, [command])
    assert out.exit_code == 0
    assert _passed_base(env, method) == "origin/main"


@command_param
def test_default_branch_read_at_most_once(
    env: Env, monkeypatch: pytest.MonkeyPatch, command: str, orchestrator_name: str, method: str, result: object
):
    calls = [0]
    host = env.host.code_host()
    original = host.repository.default_branch

    async def counting(repo: RepositoryRef) -> str:
        calls[0] += 1
        return await original(repo)

    monkeypatch.setattr(host.repository, "default_branch", counting)
    monkeypatch.setattr("dotharness.vibe_heal.cli._start_code_host", lambda _config: host)
    out = runner.invoke(app, [command])
    assert out.exit_code == 0
    assert calls[0] == 1


@command_param
def test_help_states_chain_and_default_is_none(
    env: Env, command: str, orchestrator_name: str, method: str, result: object
):
    out = runner.invoke(app, [command, "--help"])
    assert out.exit_code == 0

    import typer.main

    group = typer.main.get_command(app)
    click_command = getattr(group, "commands", {})[command]
    option = next(p for p in click_command.params if p.name == "base_branch")
    assert option.default is None
    help_text = getattr(option, "help", None)
    assert help_text is not None
    for part in ("DOTHARNESS_BASE_BRANCH", "default branch on the code host", "origin/main"):
        assert part in help_text
