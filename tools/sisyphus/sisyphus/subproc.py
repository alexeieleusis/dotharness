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


def run_command(argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None) -> CommandResult:
    """Run ``argv`` with captured text output; ``env`` entries are added to the inherited environment.

    A missing executable is reported as exit code 127 with the error text on stderr.
    """
    full = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(list(argv), cwd=cwd, env=full, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
