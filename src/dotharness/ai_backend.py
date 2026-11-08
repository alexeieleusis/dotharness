"""Shared async launcher for the ``claude`` and ``opencode`` CLIs.

One entry point, ``launch``, builds the command for the named backend, delivers the prompt through a temp file,
runs the command with ``subprocess_runner.run`` (group SIGTERM, grace, SIGKILL; also on cancellation) and returns
its ``RunResult``.

Carried over from pr-review's ``Backend`` (the global opencode lock is dropped): retries, the cwd-divergence
monitor, the opencode plugin check, the repo-identity and harness-repo guards, ``PWD`` pinned to ``cwd`` and the
wait for the process group to exit. Each is optional by parameter. See ``docs/design/shared-async-layer.md``.

Result and error convention:

* The ``RunResult`` of the last attempt is returned for a plain non-zero exit, a timeout (``timed_out``) and a
  missing executable (``not_found``); the launcher never raises for those.
* ``ValueError``: unknown backend name. ``TypeError``: ``backend_options`` of the other backend's type.
* ``OpencodePluginError``, ``BackendCwdDivergedError`` and ``RepoIdentityError`` are raised by the optional guards.

The repo and harness guards live in ``repo_guard``, the opencode plugin check and cwd monitor in
``opencode_guards``; ``launch`` composes them.

Neither argv nor env is ever logged, put in an exception message or kept in a result.
"""

import asyncio
import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotharness.opencode_guards import (
    BackendCwdDivergedError,
    MonitorState,
    assert_no_opencode_plugins,
    watch_cwd,
)
from dotharness.paths import data_dir
from dotharness.repo_guard import (
    RepoIdentityError,
    assert_harness_unchanged,
    assert_repo_identity,
    snapshot_harness_repo,
)
from dotharness.subprocess_runner import RunResult, run
from dotharness.subprocess_runner import group_alive as _group_alive  # tests patch this name

logger = logging.getLogger(__name__)


BackendName = Literal["claude", "opencode"]
BACKEND_NAMES: tuple[BackendName, ...] = ("claude", "opencode")

TMP_DIR = data_dir() / "tmp"

# How long to wait for a run's own process group to fully exit, and how often to poll.
GROUP_EXIT_WAIT_SECONDS = 30.0
GROUP_EXIT_POLL_SECONDS = 1.0
_PGREP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ClaudeOptions:
    """Flags for the ``claude`` backend. Callers choose every flag; nothing is on by default."""

    print_mode: bool = True
    print_flag: str = "-p"
    skip_permissions: bool = False
    permission_mode: str | None = None
    disable_slash_commands: bool = False
    output_format: str | None = None
    allowed_tools: str | None = None


@dataclass(frozen=True)
class OpencodeOptions:
    """Flags and guards for the ``opencode`` backend. Callers choose every flag; nothing is on by default."""

    standalone: bool = False
    auto: bool = False
    opencode_dir: str | Path | None = None
    model: str | None = None
    monitor_cwd: bool = False
    check_plugins: bool = False


BackendOptions = ClaudeOptions | OpencodeOptions


def select_backend(name: str) -> BackendName:
    """Return ``name`` if it is ``claude`` or ``opencode``; raise ``ValueError`` for anything else."""
    if name == "claude":
        return "claude"
    if name == "opencode":
        return "opencode"
    msg = f"Unknown backend: {name!r} (expected one of: {', '.join(BACKEND_NAMES)})"
    raise ValueError(msg)


def build_claude_command(
    prompt: str,
    *,
    print_mode: bool = True,
    print_flag: str = "-p",
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    disable_slash_commands: bool = False,
    output_format: str | None = None,
    allowed_tools: str | None = None,
) -> list[str]:
    """Build the ``claude`` argv. Callers choose every flag; nothing is on by default."""
    cmd = ["claude"]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if disable_slash_commands:
        cmd.append("--disable-slash-commands")
    if print_mode:
        cmd += [print_flag, prompt]
    else:
        cmd.append(prompt)
    if output_format:
        cmd += ["--output-format", output_format]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    return cmd


def build_opencode_command(
    prompt: str,
    *,
    standalone: bool = False,
    auto: bool = False,
    opencode_dir: str | Path | None = None,
    model: str | None = None,
) -> list[str]:
    """Build the ``opencode run`` argv. Callers choose every flag; nothing is on by default."""
    cmd = ["opencode", "run"]
    if standalone:
        cmd.append("--standalone")
    if auto:
        cmd.append("--auto")
    if opencode_dir:
        cmd += ["--dir", str(opencode_dir)]
    cmd.append(prompt)
    if model:
        cmd += ["-m", model]
    return cmd


def _resolve_options(backend: BackendName, options: BackendOptions | None) -> BackendOptions:
    expected = ClaudeOptions if backend == "claude" else OpencodeOptions
    if options is None:
        return expected()
    if not isinstance(options, expected):
        msg = f"{type(options).__name__} does not apply to the {backend} backend (expected {expected.__name__})"
        raise TypeError(msg)
    return options


def _build_env(cwd: str, env: Mapping[str, str] | None, path_prepend: Sequence[str]) -> dict[str, str]:
    full = os.environ.copy()
    if path_prepend:
        full["PATH"] = ":".join(path_prepend) + ":" + full.get("PATH", "")
    if env:
        full.update(env)
    # opencode trusts a stale PWD over its own getcwd(); pin it to the directory the child really starts in.
    full["PWD"] = cwd
    return full


def _write_prompt_file(prompt: str) -> Path:
    TMP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    TMP_DIR.chmod(0o700)  # prompts hold source code; keep the directory private even if it pre-existed
    # mkstemp creates the file with O_EXCL, a random name and mode 0600.
    fd, path_str = tempfile.mkstemp(suffix=".md", dir=TMP_DIR, prefix="dotharness_")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


async def _wait_for_group_exit(pgid: int, prefix: str) -> None:
    """Wait (bounded) until no process is left in ``pgid``; warn when some survive."""
    deadline = time.monotonic() + GROUP_EXIT_WAIT_SECONDS
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            logger.warning(
                "%sBackend process group %d still has member(s) %ds after the run finished, proceeding anyway",
                prefix,
                pgid,
                GROUP_EXIT_WAIT_SECONDS,
            )
            return
        await asyncio.sleep(GROUP_EXIT_POLL_SECONDS)


async def _warn_if_backend_survived(backend: BackendName, prefix: str) -> None:
    """After a timeout-kill, look for same-named processes that escaped the group (daemonised children).

    Only pids are inspected and logged: ``pgrep -a`` would print other processes' command lines.
    """
    result = await run(["pgrep", "-x", backend], timeout=_PGREP_TIMEOUT_SECONDS)
    pids = result.stdout.split()
    if result.returncode == 0 and pids:
        logger.warning(
            "%sBackend timeout-kill: %d %s process(es) still alive afterward (pids %s); the group kill may have "
            "missed a daemonised child",
            prefix,
            len(pids),
            backend,
            ", ".join(pids),
        )


def _build_command(text: str, options: BackendOptions) -> list[str]:
    if isinstance(options, ClaudeOptions):
        return build_claude_command(
            text,
            print_mode=options.print_mode,
            print_flag=options.print_flag,
            skip_permissions=options.skip_permissions,
            permission_mode=options.permission_mode,
            disable_slash_commands=options.disable_slash_commands,
            output_format=options.output_format,
            allowed_tools=options.allowed_tools,
        )
    return build_opencode_command(
        text,
        standalone=options.standalone,
        auto=options.auto,
        opencode_dir=options.opencode_dir,
        model=options.model,
    )


async def _run_monitored(
    cmd: list[str], *, cwd: str, env: dict[str, str], timeout: float, monitor_cwd: bool, prefix: str
) -> tuple[RunResult, int | None]:
    """Run ``cmd``; raise ``BackendCwdDivergedError`` if the monitor killed it. Returns the result and the pgid."""
    state = MonitorState()

    def on_start(pid: int) -> None:
        state.pid = pid
        if monitor_cwd:
            state.monitor = asyncio.create_task(watch_cwd(state, cwd, prefix))

    try:
        result = await run(cmd, cwd=cwd, env=env, timeout=timeout, on_start=on_start)
    finally:
        if state.monitor is not None:
            state.monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.monitor
    if state.diverged_to is not None:
        msg = (
            f"{prefix}backend process (pid {state.pid}) cwd diverged from {cwd} to {state.diverged_to} "
            "mid-run and was killed; its output is untrusted"
        )
        raise BackendCwdDivergedError(msg)
    return result, state.pid


async def _attempt(
    backend: BackendName,
    prompt: str,
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,  # NOSONAR python:S7483 - run() must kill the group and return RunResult(timed_out)
    prompt_via_file: bool,
    options: BackendOptions,
    prefix: str,
    expected_repo_name: str | None,
    harness: tuple[Path, str] | None,
) -> RunResult:
    prompt_file = _write_prompt_file(prompt) if prompt_via_file else None
    try:
        text = f"Read {prompt_file} and follow the instructions exactly." if prompt_file else prompt
        cmd = _build_command(text, options)
        result, pgid = await _run_monitored(
            cmd,
            cwd=cwd,
            env=env,
            timeout=timeout,
            monitor_cwd=isinstance(options, OpencodeOptions) and options.monitor_cwd,
            prefix=prefix,
        )
        if pgid is not None:
            await _wait_for_group_exit(pgid, prefix)
        if result.timed_out:
            await _warn_if_backend_survived(backend, prefix)
        elif not result.not_found and expected_repo_name is not None:
            await assert_repo_identity(cwd, expected_repo_name)
        if not result.not_found:
            await assert_harness_unchanged(harness)
        return result
    finally:
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)


async def _run_attempts(
    backend: BackendName,
    prompt: str,
    total_attempts: int,
    *,
    retry_on_nonzero: bool,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    prompt_via_file: bool,
    options: BackendOptions,
    prefix: str,
    expected_repo_name: str | None,
    harness: tuple[Path, str] | None,
) -> RunResult:
    """Run up to ``total_attempts`` attempts, stopping at the first success, ``not_found`` or (unless ``retry_on_nonzero``) non-zero exit; return the last result."""
    for attempt in range(1, total_attempts + 1):
        if expected_repo_name is not None:
            await assert_repo_identity(cwd, expected_repo_name)
        logger.info("%sRunning %s backend (attempt %d/%d, cwd=%s)", prefix, backend, attempt, total_attempts, cwd)
        result = await _attempt(
            backend,
            prompt,
            cwd=cwd,
            env=env,
            timeout=timeout,
            prompt_via_file=prompt_via_file,
            options=options,
            prefix=prefix,
            expected_repo_name=expected_repo_name,
            harness=harness,
        )
        if result.not_found or (not result.timed_out and (result.returncode == 0 or not retry_on_nonzero)):
            break
        if attempt < total_attempts:
            logger.warning(
                "%sBackend %s (exit %d), retrying (attempt %d/%d)",
                prefix,
                "timed out" if result.timed_out else "failed",
                result.returncode,
                attempt + 1,
                total_attempts,
            )
    return result


async def _log_identity_failure_after_timeout(cwd: str, expected_repo_name: str, prefix: str) -> None:
    try:
        await assert_repo_identity(cwd, expected_repo_name)
    except RepoIdentityError:
        logger.exception("%sRepo identity check failed after a timeout-kill", prefix)


async def launch(
    backend: str,
    prompt: str,
    *,
    cwd: str | Path,
    timeout: float,  # NOSONAR python:S7483 - run() must kill the group and return RunResult(timed_out)
    env: Mapping[str, str] | None = None,
    path_prepend: Sequence[str] = (),
    prompt_via_file: bool = True,
    context: str | None = None,
    max_retries: int = 0,
    retry_on_nonzero: bool = False,
    expected_repo_name: str | None = None,
    harness_repo_root: str | Path | None = None,
    backend_options: BackendOptions | None = None,
) -> RunResult:
    """Run ``prompt`` through ``backend`` (``claude`` or ``opencode``) and return the last attempt's ``RunResult``.

    ``cwd`` and ``timeout`` (seconds) are required: the launcher holds no default for either. ``env`` is added to
    the inherited environment; ``path_prepend`` is put in front of ``PATH``; ``PWD`` is always set to ``cwd``.
    With ``prompt_via_file`` (default) the prompt is written to a temp file under ``TMP_DIR`` and the backend is
    told to read it; the file is removed after every attempt, also on cancellation.

    ``max_retries``: extra attempts after a timeout; also after a non-zero exit only with ``retry_on_nonzero=True``
    (default off: a failed run may already have had side effects such as posted replies or pushed commits, so
    re-running it is opt-in). Never after ``not_found``. Guards:
    ``expected_repo_name`` (``owner/repo``) checks the identity of ``cwd`` before and after each attempt;
    a non-None ``harness_repo_root`` checks that checkout's HEAD did not move;
    ``OpencodeOptions.monitor_cwd`` kills the run if its real cwd diverges; ``OpencodeOptions.check_plugins``
    raises ``OpencodePluginError`` when ``opencode plugin list`` reports plugins.

    ``backend_options`` is a ``ClaudeOptions`` or ``OpencodeOptions`` (default: all flags off); the type must match
    ``backend`` or ``TypeError`` is raised. See the module docstring for the result and error convention.
    """
    name = select_backend(backend)
    options = _resolve_options(name, backend_options)

    cwd_str = str(cwd)
    prefix = f"{context}: " if context else ""
    full_env = _build_env(cwd_str, env, path_prepend)
    if isinstance(options, OpencodeOptions) and options.check_plugins:
        await assert_no_opencode_plugins(cwd_str, full_env)

    # Snapshot once: re-snapshotting per attempt would adopt a HEAD an earlier attempt moved as the new baseline.
    harness = None
    if harness_repo_root is not None:
        harness = await snapshot_harness_repo(Path(cwd_str), Path(harness_repo_root))
    result = await _run_attempts(
        name,
        prompt,
        max_retries + 1,
        retry_on_nonzero=retry_on_nonzero,
        cwd=cwd_str,
        env=full_env,
        timeout=timeout,
        prompt_via_file=prompt_via_file,
        options=options,
        prefix=prefix,
        expected_repo_name=expected_repo_name,
        harness=harness,
    )
    if result.timed_out and expected_repo_name is not None:
        await _log_identity_failure_after_timeout(cwd_str, expected_repo_name, prefix)
    return result
