from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from spec_prism_flow.build.errors import MergeGateFailure


def run_merge_gates(cwd: Path, commands: list[str], *, tail_lines: int = 40) -> None:
    for cmd in commands:
        try:
            result = subprocess.run(  # noqa: S603
                shlex.split(cmd), cwd=cwd, capture_output=True, text=True, check=False
            )
        except (OSError, IndexError, ValueError) as e:
            raise MergeGateFailure(cmd, str(e)) from e
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            tail = "\n".join(combined.splitlines()[-tail_lines:])
            raise MergeGateFailure(cmd, tail)
