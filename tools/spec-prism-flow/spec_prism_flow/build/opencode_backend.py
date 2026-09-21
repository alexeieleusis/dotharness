from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from pathlib import Path

from spec_prism_flow.build import agent_instructions
from spec_prism_flow.build.errors import CommandError

DEFAULT_TIMEOUT_SECONDS = 1800
_KILL_GRACE_SECONDS = 5


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
        tmp_path = agent_instructions.write_instructions_file(instructions)
        cmd = self._build_command(tmp_path, cwd)
        try:
            proc = self._start_process(cmd, cwd)
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                raise
        finally:
            tmp_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            raise OpencodeCommandError(cmd, proc.returncode, stderr.decode("utf-8", errors="replace"))
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _build_command(tmp_path: Path, cwd: Path) -> list[str]:
        prompt = agent_instructions.read_prompt(tmp_path)
        return ["opencode", "run", prompt, "--pure", "--dir", str(cwd)]

    @staticmethod
    def _start_process(cmd: list[str], cwd: Path) -> subprocess.Popen:
        """Split out from invoke() as its own seam so tests can mock just the
        opencode-invoking subprocess, mirroring ClaudeBackend._start_process."""
        return subprocess.Popen(  # noqa: S603
            cmd,
            cwd=cwd,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        """Kill the whole process group opencode was started in, not just the
        opencode process itself -- mirrors ClaudeBackend._kill. Without this,
        subprocess.run's built-in timeout handling only killed the immediate
        opencode process, leaving any child processes it spawned (git, shell tool
        calls, etc.) running unparented in cwd past the timeout."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_KILL_GRACE_SECONDS)
