"""opencode-specific run guards: the plugin check and the cwd-divergence monitor.

Used by ``ai_backend.launch`` when ``OpencodeOptions.check_plugins`` / ``monitor_cwd`` are set.
"""

import asyncio
import logging
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotharness.subprocess_runner import killpg, run

logger = logging.getLogger(__name__)

# How often to compare a running opencode process's kernel-level cwd with the directory it was launched in.
CWD_MONITOR_POLL_SECONDS = 3.0
_HELPER_TIMEOUT_SECONDS = 30.0
_LSOF_TIMEOUT_SECONDS = 10.0


class OpencodePluginError(RuntimeError):
    """opencode has a plugin installed, which can act independently of the session isolation ``--standalone`` gives."""


class BackendCwdDivergedError(RuntimeError):
    """The backend process's kernel-level cwd no longer matched the directory it was launched in.

    Its output cannot be trusted, so the process is killed rather than let finish.
    """


@dataclass
class MonitorState:
    """Mutable per-attempt state shared between the runner callback and the monitor."""

    pid: int | None = None
    monitor: "asyncio.Task[None] | None" = None
    diverged_to: str | None = None


async def _lsof_cwd(pid: int) -> str | None:
    """The kernel-level cwd of ``pid`` (not the PWD env var); None if the process is gone or lsof fails."""
    result = await run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=_LSOF_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


async def watch_cwd(state: MonitorState, expected_cwd: str, prefix: str) -> None:
    expected = str(Path(expected_cwd).resolve())
    while True:
        await asyncio.sleep(CWD_MONITOR_POLL_SECONDS)
        if state.pid is None:
            return
        actual = await _lsof_cwd(state.pid)
        if actual is not None and actual != expected:
            state.diverged_to = actual
            logger.error(
                "%sBackend process (pid %d) cwd diverged from %s to %s mid-run, killing it; its output "
                "cannot be trusted",
                prefix,
                state.pid,
                expected,
                actual,
            )
            killpg(state.pid, signal.SIGKILL)
            return


async def assert_no_opencode_plugins(cwd: str | Path, env: Mapping[str, str] | None = None) -> None:
    """Raise ``OpencodePluginError`` unless ``opencode plugin list`` reports no plugins in ``cwd``.

    ``--standalone`` isolates sessions but not a globally installed plugin, which can mutate git state on its own.
    Plugins are configured per project, so this must run with the same ``cwd`` as the backend run.
    """
    result = await run(
        ["opencode", "plugin", "list"],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        timeout=_HELPER_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        msg = (
            f"could not verify opencode has no plugins installed ('opencode plugin list' exited "
            f"{result.returncode}): {result.stderr.strip()}"
        )
        raise OpencodePluginError(msg)
    if result.stdout.strip().lower() != "no plugins found":
        msg = (
            "opencode has one or more plugins installed; refusing to run the opencode backend, since a plugin can "
            f"act independently of --standalone's session isolation ('opencode plugin list' reported):\n"
            f"{result.stdout.strip()}"
        )
        raise OpencodePluginError(msg)
