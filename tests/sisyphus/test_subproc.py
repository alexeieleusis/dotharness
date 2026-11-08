"""run_command must always return a CommandResult, never raise on odd child output."""

from __future__ import annotations

import sys

from dotharness.sisyphus.subproc import run_command


async def test_undecodable_output_is_replaced_not_raised() -> None:
    result = await run_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"])
    assert result.returncode == 0
    assert result.stdout == "\ufffd"


async def test_missing_executable_returns_127() -> None:
    assert (await run_command(["definitely-not-a-real-binary"])).returncode == 127


async def test_timeout_returns_nonzero_result() -> None:
    result = await run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert result.returncode == 124
    assert "timed out" in result.stderr


async def test_env_is_added_to_inherited_environment(monkeypatch) -> None:
    monkeypatch.setenv("SISYPHUS_INHERITED", "a")
    code = "import os; print(os.environ['SISYPHUS_INHERITED'], os.environ['SISYPHUS_ADDED'])"
    result = await run_command([sys.executable, "-c", code], env={"SISYPHUS_ADDED": "b"})
    assert result.stdout.strip() == "a b"


async def test_cwd_and_nonzero_status() -> None:
    import os
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        code = "import os, sys; print(os.getcwd()); sys.exit(3)"
        result = await run_command([sys.executable, "-c", code], Path(d))
    assert result.returncode == 3
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(d)
