import pytest
from typer.testing import CliRunner

from dotharness import cli

NAMES = ["pr_review", "spec_prism_flow", "sisyphus"]


@pytest.mark.parametrize("name", NAMES)
def test_stub_prints_message_and_exits_zero(runner: CliRunner, name: str) -> None:
    result = runner.invoke(getattr(cli, name), [])
    assert result.exit_code == 0
    assert result.output.strip() == f"{name}: not yet migrated, see docs/migration-ledger.md"
