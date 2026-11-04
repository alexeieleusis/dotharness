"""AI tool utilities."""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

import aiofiles


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
    except (asyncio.TimeoutError, asyncio.CancelledError):
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


async def write_prompt_file(prompt: str, *, suffix: str = ".txt") -> Path:
    """Write a prompt to a temp file in the current working directory.

    Created in cwd (not the system temp dir) so sandboxed AI CLI tools that
    restrict file access to the working directory can still read it.

    Args:
        prompt: The prompt content to write.
        suffix: File suffix for the temp file.

    Returns:
        Path to the written temp file.
    """
    fd, path_str = tempfile.mkstemp(suffix=suffix, dir=Path.cwd())
    os.close(fd)
    path = Path(path_str)
    try:
        async with aiofiles.open(path, mode="w", encoding="utf-8") as f:
            await f.write(prompt)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


@asynccontextmanager
async def temp_prompt_file(prompt: str, *, suffix: str = ".txt") -> AsyncIterator[Path]:
    """Write a prompt to a temp file and clean it up afterward.

    Args:
        prompt: The prompt content to write.
        suffix: File suffix for the temp file.

    Yields:
        Path to the temp file, deleted on exit.
    """
    path = await write_prompt_file(prompt, suffix=suffix)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def build_file_prompt(temp_path: Path) -> str:
    """Build a short CLI-arg instruction referencing a temp prompt file.

    Args:
        temp_path: Path to the temp file holding the full prompt.

    Returns:
        Short instruction string referencing the file by its basename.
    """
    return f'Read "{temp_path.name}" and follow the instructions in that file exactly.'
