"""AI tool utilities."""

import math
from pathlib import Path
from typing import NamedTuple

from dotharness.subprocess_runner import run


class CommandResult(NamedTuple):
    """Result of an external command."""

    success: bool
    stdout: str
    stderr: str
    exit_code: int | None = None


async def run_command(
    cmd: list[str],
) -> CommandResult:
    """Run an external command and capture its output.

    Callers should wrap this with `asyncio.timeout()` if a deadline is needed.

    Args:
        cmd: The command to execute, as a list of strings.

    Returns:
        A CommandResult with the outcome.
    """
    # No own deadline: callers bound the call with asyncio.timeout(), which cancels run() and kills the group.
    result = await run(cmd, cwd=Path.cwd(), timeout=math.inf)
    if result.not_found:
        raise FileNotFoundError(result.stderr)
    return CommandResult(
        success=result.returncode == 0,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
    )
