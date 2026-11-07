"""AI tool utilities."""

import asyncio
from pathlib import Path
from typing import NamedTuple


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
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=Path.cwd(),
    )

    try:
        stdout_bytes, stderr_bytes = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise

    stdout = stdout_bytes.decode() if stdout_bytes else ""
    stderr = stderr_bytes.decode() if stderr_bytes else ""

    return CommandResult(
        success=process.returncode == 0,
        stdout=stdout,
        stderr=stderr,
        exit_code=process.returncode,
    )
