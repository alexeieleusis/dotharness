"""One subprocess result type and one runner, shared by the orchestrator and the pick clients (stdlib only)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess call."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode("utf-8", errors="replace")


def run_command(
    argv: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """Run ``argv`` with captured UTF-8 text output; ``env`` entries are added to the inherited environment.

    Undecodable bytes are replaced rather than raised. A missing executable is reported as exit code 127 and a
    timeout as exit code 124, each with the error text on stderr.
    """
    full = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(  # noqa: S603
            list(argv),
            cwd=cwd,
            env=full,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return CommandResult(124, _text(exc.stdout), _text(exc.stderr) + f"timed out after {timeout}s")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
