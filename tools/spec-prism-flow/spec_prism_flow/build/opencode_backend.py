from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from spec_prism_flow.build.errors import CommandError

DEFAULT_TIMEOUT_SECONDS = 1800


class OpencodeCommandError(CommandError):
    """Raised when the `opencode run` subprocess exits non-zero."""


class OpencodeBackend:
    """Generalizes orchestrate's opencode_runner.py.

    Deliberately does NOT support an expected_repo_name / repo-identity guard --
    unlike ClaudeBackend, this phase's requirements only ask for one on the Claude
    Code backend (7.2), not this one (7.3).

    --dangerously-skip-permissions is never passed: confirmed a hard failure on the
    installed opencode version. This is a permanent divergence from ClaudeBackend,
    not an oversight to "fix" toward symmetry.
    """

    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def invoke(self, instructions: str, cwd: Path) -> str:
        fd, path_str = tempfile.mkstemp(suffix=".md", prefix="spec_prism_flow_")
        tmp_path = Path(path_str)
        os.close(fd)
        cmd = self._build_command(tmp_path, cwd)
        try:
            tmp_path.write_text(instructions, encoding="utf-8")
            result = subprocess.run(  # noqa: S603
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        if result.returncode != 0:
            raise OpencodeCommandError(cmd, result.returncode, result.stderr)
        return result.stdout

    @staticmethod
    def _build_command(tmp_path: Path, cwd: Path) -> list[str]:
        prompt = f"Read {tmp_path} and follow the instructions exactly."
        return ["opencode", "run", prompt, "--pure", "--dir", str(cwd)]
