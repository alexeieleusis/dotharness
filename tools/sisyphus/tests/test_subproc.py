"""run_command must always return a CommandResult, never raise on odd child output."""

from __future__ import annotations

import sys

from sisyphus.subproc import run_command


def test_undecodable_output_is_replaced_not_raised() -> None:
    result = run_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"])
    assert result.returncode == 0
    assert result.stdout == "\ufffd"


def test_missing_executable_returns_127() -> None:
    assert run_command(["definitely-not-a-real-binary"]).returncode == 127


def test_timeout_returns_nonzero_result() -> None:
    result = run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert result.returncode == 124
    assert "timed out" in result.stderr
