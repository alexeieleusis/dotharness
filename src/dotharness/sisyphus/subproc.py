"""Subprocess and agent-launch adapters over the shared layers (sync wrappers; never call from a running event loop).

``run_command`` wraps :func:`dotharness.subprocess_runner.run` and keeps the ``CommandResult`` contract the
orchestrator and the pick clients use. ``make_agent_runner`` wraps :func:`dotharness.ai_backend.launch_sync` in the
style of ``dotharness.pr_review.backend.Backend``. Both use ``asyncio.run`` underneath. Neither argv, env, the prompt
nor backend output is logged here, and none is put into an exception text.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotharness.ai_backend import ClaudeOptions, OpencodeOptions, RunResult, launch_sync
from dotharness.sisyphus.config import ConfigError
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


class AgentError(Exception):
    """The agent backend exited non-zero, timed out or could not be started."""


AgentRunner = Callable[[str], None]
Launcher = Callable[..., RunResult]


def make_agent_runner(
    backend: str | None,
    cwd: Path,
    run: Launcher = launch_sync,
    *,
    timeout_minutes: float,
) -> AgentRunner:
    """Agent runner for the configured backend, run in ``cwd`` (the dedicated clone).

    ``claude`` runs with ``--dangerously-skip-permissions`` (accepted risk: the boundary is the dedicated clone and
    the push guard); ``opencode`` runs ``opencode run <prompt>`` with no extra options. ``run`` is the injectable
    launcher (default ``launch_sync``; tests pass a fake with the same signature). A run past ``timeout_minutes``,
    a backend that cannot start or a non-zero exit raises :class:`AgentError`, so a hung agent stops at a checkpoint
    and the clone lock is released. Errors raised by the shared layer itself (``ValueError``, ``TypeError``) propagate.
    """
    options: Any
    if backend == "claude":
        options = ClaudeOptions(skip_permissions=True)
    elif backend == "opencode":
        options = OpencodeOptions()
    else:
        raise ConfigError("[agent] backend must be set to 'claude' or 'opencode' to run an agent")

    def runner(prompt: str) -> None:
        result = run(backend, prompt, cwd=cwd, timeout=timeout_minutes * 60, backend_options=options)
        if result.timed_out:
            raise AgentError(f"the {backend} backend timed out after {timeout_minutes} minutes")
        if result.not_found:
            raise AgentError(f"cannot start the {backend} backend: {result.stderr}")
        if result.returncode != 0:
            raise AgentError(f"the {backend} backend exited with status {result.returncode}")

    return runner
