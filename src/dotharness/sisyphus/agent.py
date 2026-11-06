"""Agent-launch adapter over the shared layer (sync wrapper; never call from a running event loop).

``make_agent_runner`` wraps :func:`dotharness.ai_backend.launch_sync` in the style of
``dotharness.pr_review.backend.Backend``. Neither the prompt nor backend output is logged here, and none is put into
an exception text.
"""

# ruff: noqa: TRY003
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotharness.ai_backend import ClaudeOptions, OpencodeOptions, RunResult, launch_sync
from dotharness.sisyphus.config import ConfigError


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
