from click.testing import CliRunner

from spec_prism_flow.cli import cli


def test_cli_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "plan" in result.output
    assert "build" in result.output


def test_plan_and_build_are_registered_groups() -> None:
    runner = CliRunner()

    assert runner.invoke(cli, ["plan", "--help"]).exit_code == 0
    assert runner.invoke(cli, ["build", "--help"]).exit_code == 0
