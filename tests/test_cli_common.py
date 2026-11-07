import pytest
import typer

from dotharness.cli_common import fail


def test_fail_prints_error_to_stderr_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        fail("boom")

    assert exc_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == "Error: boom\n"
    assert captured.out == ""
