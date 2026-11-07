"""Subprocess adapter over the shared layer (sync wrapper; never call from a running event loop).

``run_command`` wraps :func:`dotharness.subprocess_runner.run` and keeps the ``CommandResult`` contract the
orchestrator and the pick clients use. Neither argv nor env is logged here. The agent launcher lives in
``dotharness.sisyphus.agent``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotharness.subprocess_runner import run

# The shared runner requires a timeout. Callers that passed none (git, gate commands, ``gh``) get this bound.
DEFAULT_COMMAND_TIMEOUT = 3600.0


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess call."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_command(
    argv: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """Run ``argv`` with captured text output; ``env`` entries are added to the inherited environment.

    Undecodable bytes are replaced rather than raised. A missing executable is reported as exit code 127 and a
    timeout (``DEFAULT_COMMAND_TIMEOUT`` when none is given) as exit code 124 with ``timed out after {timeout}s`` on
    stderr. The child runs in its own process group, which is killed on timeout (SIGTERM, then SIGKILL after 5 s).
    """
    result = asyncio.run(
        run(
            list(argv),
            cwd=cwd,
            env={**os.environ, **env} if env else None,
            timeout=DEFAULT_COMMAND_TIMEOUT if timeout is None else timeout,
        )
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)
