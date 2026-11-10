"""Tests for the code host settings and the CLI startup that uses them."""

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dotharness.code_host import CodeHost, HostSelection
from dotharness.vibe_heal import cli
from dotharness.vibe_heal.cli import app
from dotharness.vibe_heal.config import InvalidConfigurationError, VibeHealConfig

# The shared conftest replaces the startup with an in-memory host unless a module opts out.
REAL_CODE_HOST_STARTUP = True

runner = CliRunner()

SECRET = "s3cr3t-token-value"  # noqa: S105


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("DOTHARNESS_CODE_HOST", "DOTHARNESS_CODE_HOST_OPTIONS", "CODE_HOST", "CODE_HOST_OPTIONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONARQUBE_URL", "https://sonar.example.com")
    monkeypatch.setenv("SONARQUBE_TOKEN", "tok")
    monkeypatch.setenv("SONARQUBE_PROJECT_KEY", "proj")


class TestFields:
    def test_defaults(self) -> None:
        config = VibeHealConfig()
        assert config.code_host is None
        assert config.code_host_options == {}

    def test_aliases_are_read_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST", "reference")
        monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", '{"a": 1}')
        config = VibeHealConfig()
        assert config.code_host == "reference"
        assert config.code_host_options == {"a": 1}

    def test_bare_names_are_not_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODE_HOST", "other")
        monkeypatch.setenv("CODE_HOST_OPTIONS", '{"a": 1}')
        config = VibeHealConfig()
        assert config.code_host is None
        assert config.code_host_options == {}

    def test_read_from_env_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / "custom.env"
        env_file.write_text('DOTHARNESS_CODE_HOST=mod:attr\nDOTHARNESS_CODE_HOST_OPTIONS={"k": "v"}\n')
        config = VibeHealConfig(env_file=str(env_file))
        assert config.code_host == "mod:attr"
        assert config.code_host_options == {"k": "v"}

    def test_empty_options_string_is_empty_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", "")
        assert VibeHealConfig().code_host_options == {}

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("{not json", "value is not valid JSON"),
            ("[1, 2]", "value is not a JSON object"),
            ('"text"', "value is not a JSON object"),
        ],
    )
    def test_bad_options_message_names_field_and_kind_only(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, reason: str
    ) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", raw)
        with pytest.raises(InvalidConfigurationError) as info:
            VibeHealConfig()
        assert str(info.value) == f'code host setting DOTHARNESS_CODE_HOST_OPTIONS = "<redacted>": {reason}'
        assert info.value.__cause__ is None

    def test_bad_options_never_echo_the_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", f'{{"token": "{SECRET}"')
        with pytest.raises(InvalidConfigurationError) as info:
            VibeHealConfig()
        assert SECRET not in str(info.value)
        assert SECRET not in repr(info.value)


class TestStartup:
    def test_absent_name_selects_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[HostSelection] = []
        real = cli.create_code_host

        def spy(selection: HostSelection) -> CodeHost:
            seen.append(selection)
            return real(selection)

        monkeypatch.setattr(cli, "create_code_host", spy)
        host = cli._start_code_host(VibeHealConfig())

        assert seen == [HostSelection(setting="DOTHARNESS_CODE_HOST", name=None, options={}, token_cmd=None)]
        assert host.declaration.anchors.requires_line_in_diff

    def test_selection_carries_name_and_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST", "reference")
        monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", "{}")
        seen: list[HostSelection] = []
        monkeypatch.setattr(cli, "create_code_host", lambda selection: seen.append(selection))
        cli._start_code_host(VibeHealConfig())
        assert seen[0].name == "reference"
        assert seen[0].options == {}
        assert seen[0].token_cmd is None

    def test_unknown_host_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST", "nope")
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 1
        assert 'Configuration error: code host setting DOTHARNESS_CODE_HOST = "nope": ' in " ".join(
            result.stdout.split()
        )

    def test_bad_options_stop_the_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", "{oops")
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 1
        assert (
            'Configuration error: code host setting DOTHARNESS_CODE_HOST_OPTIONS = "<redacted>": '
            "value is not valid JSON"
        ) in " ".join(result.stdout.split())

    def test_post_mode_uses_environment_without_sonarqube_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("SONARQUBE_URL", "SONARQUBE_TOKEN", "SONARQUBE_PROJECT_KEY"):
            monkeypatch.delenv(name)
        monkeypatch.setenv("DOTHARNESS_CODE_HOST", "nope")
        result = runner.invoke(app, ["review", "--post", "--report-file", "/nonexistent/review.json"])
        assert result.exit_code == 1
        assert 'Configuration error: code host setting DOTHARNESS_CODE_HOST = "nope": ' in result.stdout

    def test_post_mode_bad_options_without_sonarqube_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("SONARQUBE_URL", "SONARQUBE_TOKEN", "SONARQUBE_PROJECT_KEY"):
            monkeypatch.delenv(name)
        monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", "[]")
        result = runner.invoke(app, ["review", "--post", "--report-file", "/nonexistent/review.json"])
        assert result.exit_code == 1
        assert "value is not a JSON object" in result.stdout

    @pytest.mark.parametrize(
        "args",
        [
            ["fix", "a.py"],
            ["dedupe", "a.py"],
            ["cleanup"],
            ["cleanup-pr"],
            ["dedupe-branch"],
            ["prune-projects"],
            ["review"],
            ["review", "--baseline"],
            ["review", "--post", "--report-file", "/nonexistent/review.json"],
            ["config"],
            ["debug-issues"],
        ],
    )
    def test_every_command_stops_on_unknown_host(self, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
        monkeypatch.setenv("DOTHARNESS_CODE_HOST", "nope")
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "Configuration error: code host setting DOTHARNESS_CODE_HOST" in result.stdout


def _names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return {n for elt in node.elts for n in _names(elt)}
    if isinstance(node, ast.Name):
        return {node.id}
    return set()


class TestHandlers:
    def test_every_configuration_error_handler_catches_the_code_host_class(self) -> None:
        tree = ast.parse(Path(cli.__file__).read_text())
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and "ConfigurationError" in _names(node.type)
        ]
        assert len(handlers) >= 10
        for handler in handlers:
            assert _names(handler.type) == {"ConfigurationError", "CodeHostConfigurationError"}, (
                f"line {handler.lineno}"
            )

    def test_no_handler_still_names_the_old_pull_request_error(self) -> None:
        source = Path(cli.__file__).read_text()
        assert "NoOpenPrError" not in source
        assert "GitHubReviewClient" not in source
