from typer.testing import CliRunner

from dotharness.spec_prism_flow.cli import cli


def test_build_group_is_not_registered(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["build", "run"])

    assert result.exit_code == 2
    assert "No such command" in result.output
