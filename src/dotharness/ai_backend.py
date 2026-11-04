"""Shared async launcher for the ``claude`` and ``opencode`` CLIs.

One entry point, ``launch``, builds the command for the named backend, delivers the prompt through a temp file,
runs the command with ``subprocess_runner.run`` (group SIGTERM, grace, SIGKILL; also on cancellation) and returns
its ``RunResult``. ``launch_sync`` is the thin ``asyncio.run`` adapter for sync callers.

Carried over from pr-review's ``Backend`` (the global opencode lock is dropped): retries, the cwd-divergence
monitor, the opencode plugin check, the repo-identity and harness-repo guards, ``PWD`` pinned to ``cwd`` and the
wait for the process group to exit. Each is optional by parameter. See ``docs/design/shared-async-layer.md``.

Result and error convention:

* The ``RunResult`` of the last attempt is returned for a plain non-zero exit, a timeout (``timed_out``) and a
  missing executable (``not_found``); the launcher never raises for those.
* ``ValueError``: unknown backend name, or an option that belongs to the other backend.
* ``OpencodePluginError``, ``BackendCwdDivergedError`` and ``RepoIdentityError`` are raised by the optional guards.

Neither argv nor env is ever logged, put in an exception message or kept in a result.
"""

import asyncio
import contextlib
import functools
import logging
import os
import signal
import tempfile
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from dotharness.subprocess_runner import RunResult, run

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

BackendName = Literal["claude", "opencode"]
BACKEND_NAMES: tuple[BackendName, ...] = ("claude", "opencode")

TMP_DIR = Path.home() / ".local/share/dotharness/tmp"

# How long to wait for a run's own process group to fully exit, and how often to poll.
GROUP_EXIT_WAIT_SECONDS = 30.0
GROUP_EXIT_POLL_SECONDS = 1.0
# How often to compare a running opencode process's kernel-level cwd with the directory it was launched in.
CWD_MONITOR_POLL_SECONDS = 3.0
_HELPER_TIMEOUT_SECONDS = 30.0
_LSOF_TIMEOUT_SECONDS = 10.0

_CLAUDE_ONLY = ("skip_permissions", "permission_mode", "disable_slash_commands", "output_format", "allowed_tools")
_OPENCODE_ONLY = ("standalone", "auto", "opencode_dir", "model", "monitor_cwd", "check_plugins")


class OpencodePluginError(RuntimeError):
    """opencode has a plugin installed, which can act independently of the session isolation ``--standalone`` gives."""


class BackendCwdDivergedError(RuntimeError):
    """The backend process's kernel-level cwd no longer matched the directory it was launched in.

    Its output cannot be trusted, so the process is killed rather than let finish.
    """


class RepoIdentityError(RuntimeError):
    """``cwd`` is not the git repo ``expected_repo_name`` says it is, or the harness repo's HEAD moved."""


@dataclass
class _Run:
    """Mutable per-attempt state shared between the runner callback and the monitor."""

    pid: int | None = None
    monitor: "asyncio.Task[None] | None" = None
    diverged_to: str | None = None


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


def _validate_options(backend: BackendName, options: Mapping[str, object]) -> None:
    foreign = _OPENCODE_ONLY if backend == "claude" else _CLAUDE_ONLY
    bad = [name for name in foreign if options.get(name)]
    if bad:
        msg = f"Option(s) {', '.join(bad)} do not apply to the {backend} backend"
        raise ValueError(msg)


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
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, path_str = tempfile.mkstemp(suffix=".md", dir=TMP_DIR, prefix="dotharness_")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _killpg(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    result = await run(["pgrep", "-x", backend], timeout=_LSOF_TIMEOUT_SECONDS)
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


async def _lsof_cwd(pid: int) -> str | None:
    """The kernel-level cwd of ``pid`` (not the PWD env var); None if the process is gone or lsof fails."""
    result = await run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=_LSOF_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


async def _watch_cwd(state: _Run, expected_cwd: str, prefix: str) -> None:
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
            _killpg(state.pid, signal.SIGKILL)
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


async def _git(working_dir: Path, *args: str) -> RunResult:
    return await run(["git", "-C", str(working_dir), *args], timeout=_HELPER_TIMEOUT_SECONDS)


_ssh_hosts: dict[str, str] = {}


async def _resolve_ssh_host(host: str) -> str:
    """Resolve an SSH config host alias to its real HostName; fall back to the alias (fail closed)."""
    if host.lower() == "github.com":
        return "github.com"
    if host in _ssh_hosts:
        return _ssh_hosts[host]
    resolved = host
    result = await run(["ssh", "-G", host], timeout=_HELPER_TIMEOUT_SECONDS)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("hostname "):
                resolved = line.split(maxsplit=1)[1].strip()
                break
    _ssh_hosts[host] = resolved
    return resolved


def _split_host_and_path(url: str) -> tuple[str, str] | None:
    normalized = url.strip().removesuffix(".git").rstrip("/")
    if normalized.startswith(("https://", "ssh://")):
        authority, sep, path = normalized.split("://", 1)[1].partition("/")
    else:
        authority, sep, path = normalized.partition(":")  # scp-style [user@]host:path
    if not sep or not path:
        return None
    return authority.rpartition("@")[2] or authority, path


async def _origin_matches_repo(url: str, repo_name: str) -> bool:
    split = _split_host_and_path(url)
    if split is None:
        return False
    host, path = split
    return (await _resolve_ssh_host(host)).lower() == "github.com" and path == repo_name


async def assert_repo_identity(working_dir: str | Path, expected_repo_name: str) -> None:
    """Raise ``RepoIdentityError`` unless ``working_dir`` is a git toplevel whose ``origin`` is ``expected_repo_name``.

    ``expected_repo_name`` is an ``owner/repo`` GitHub slug.
    """
    directory = Path(working_dir).resolve()
    toplevel = await _git(directory, "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        msg = f"{directory} is not inside a git repository ({toplevel.stderr.strip()})"
        raise RepoIdentityError(msg)
    actual = Path(toplevel.stdout.strip()).resolve()
    if actual != directory:
        msg = f"{directory} is not itself the toplevel of its git repo (toplevel is {actual})"
        raise RepoIdentityError(msg)
    origin = await _git(directory, "remote", "get-url", "origin")
    if origin.returncode != 0:
        msg = f"{directory} has no 'origin' remote ({origin.stderr.strip()})"
        raise RepoIdentityError(msg)
    if not await _origin_matches_repo(origin.stdout.strip(), expected_repo_name):
        msg = f"{directory}'s origin does not match the expected repo {expected_repo_name!r}"
        raise RepoIdentityError(msg)


async def _head_sha(working_dir: Path) -> str:
    result = await _git(working_dir, "rev-parse", "HEAD")
    if result.returncode != 0:
        msg = f"could not read HEAD for {working_dir}: {result.stderr.strip()}"
        raise RepoIdentityError(msg)
    return result.stdout.strip()


async def _discover_repo_root(start: Path) -> Path | None:
    result = await _git(start, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


async def _snapshot_harness_repo(cwd: Path, root: Path | None) -> tuple[Path, str] | None:
    """(root, HEAD) to watch, or None when there is nothing to watch (no repo found, or ``cwd`` is that repo)."""
    if root is None:
        root = await _discover_repo_root(Path(__file__).resolve().parent)
    if root is None or cwd.resolve() == root.resolve():
        return None
    return root, await _head_sha(root)


async def _assert_harness_unchanged(snapshot: tuple[Path, str] | None) -> None:
    if snapshot is None:
        return
    root, head = snapshot
    current = await _head_sha(root)
    if current != head:
        msg = f"{root}'s HEAD moved from {head} to {current} during a backend run that was not supposed to touch it"
        raise RepoIdentityError(msg)


def _build_command(backend: BackendName, text: str, options: Mapping[str, object]) -> list[str]:
    if backend == "claude":
        return build_claude_command(
            text,
            print_mode=bool(options["print_mode"]),
            print_flag=str(options["print_flag"]),
            skip_permissions=bool(options["skip_permissions"]),
            permission_mode=_opt_str(options["permission_mode"]),
            disable_slash_commands=bool(options["disable_slash_commands"]),
            output_format=_opt_str(options["output_format"]),
            allowed_tools=_opt_str(options["allowed_tools"]),
        )
    return build_opencode_command(
        text,
        standalone=bool(options["standalone"]),
        auto=bool(options["auto"]),
        opencode_dir=_opt_str(options["opencode_dir"]),
        model=_opt_str(options["model"]),
    )


async def _run_monitored(
    cmd: list[str], *, cwd: str, env: dict[str, str], timeout: float, monitor_cwd: bool, prefix: str
) -> tuple[RunResult, int | None]:
    """Run ``cmd``; raise ``BackendCwdDivergedError`` if the monitor killed it. Returns the result and the pgid."""
    state = _Run()

    def on_start(pid: int) -> None:
        state.pid = pid
        if monitor_cwd:
            state.monitor = asyncio.create_task(_watch_cwd(state, cwd, prefix))

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
    timeout: float,
    prompt_via_file: bool,
    options: Mapping[str, object],
    prefix: str,
    expected_repo_name: str | None,
    harness: tuple[Path, str] | None,
) -> RunResult:
    prompt_file = _write_prompt_file(prompt) if prompt_via_file else None
    try:
        text = f"Read {prompt_file} and follow the instructions exactly." if prompt_file else prompt
        cmd = _build_command(backend, text, options)
        result, pgid = await _run_monitored(
            cmd, cwd=cwd, env=env, timeout=timeout, monitor_cwd=bool(options["monitor_cwd"]), prefix=prefix
        )
        if pgid is not None:
            await _wait_for_group_exit(pgid, prefix)
        if result.timed_out:
            await _warn_if_backend_survived(backend, prefix)
        elif not result.not_found:
            if expected_repo_name is not None:
                await assert_repo_identity(cwd, expected_repo_name)
            await _assert_harness_unchanged(harness)
        return result
    finally:
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


async def launch(
    backend: str,
    prompt: str,
    *,
    cwd: str | Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    path_prepend: Sequence[str] = (),
    prompt_via_file: bool = True,
    context: str | None = None,
    max_retries: int = 0,
    expected_repo_name: str | None = None,
    guard_harness_repo: bool = False,
    harness_repo_root: str | Path | None = None,
    # claude only
    print_mode: bool = True,
    print_flag: str = "-p",
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    disable_slash_commands: bool = False,
    output_format: str | None = None,
    allowed_tools: str | None = None,
    # opencode only
    standalone: bool = False,
    auto: bool = False,
    opencode_dir: str | Path | None = None,
    model: str | None = None,
    monitor_cwd: bool = False,
    check_plugins: bool = False,
) -> RunResult:
    """Run ``prompt`` through ``backend`` (``claude`` or ``opencode``) and return the last attempt's ``RunResult``.

    ``cwd`` and ``timeout`` (seconds) are required: the launcher holds no default for either. ``env`` is added to
    the inherited environment; ``path_prepend`` is put in front of ``PATH``; ``PWD`` is always set to ``cwd``.
    With ``prompt_via_file`` (default) the prompt is written to a temp file under ``TMP_DIR`` and the backend is
    told to read it; the file is removed after every attempt, also on cancellation.

    ``max_retries``: extra attempts after a timeout or a non-zero exit (not after ``not_found``). Guards:
    ``expected_repo_name`` (``owner/repo``) checks the identity of ``cwd`` before and after each attempt;
    ``guard_harness_repo`` checks this checkout's HEAD did not move (``harness_repo_root`` overrides discovery);
    ``monitor_cwd`` (opencode) kills the run if its real cwd diverges; ``check_plugins`` (opencode) raises
    ``OpencodePluginError`` when ``opencode plugin list`` reports plugins. Flags of the other backend raise
    ``ValueError``. See the module docstring for the result and error convention.
    """
    name = select_backend(backend)
    options: dict[str, object] = {
        "print_mode": print_mode,
        "print_flag": print_flag,
        "skip_permissions": skip_permissions,
        "permission_mode": permission_mode,
        "disable_slash_commands": disable_slash_commands,
        "output_format": output_format,
        "allowed_tools": allowed_tools,
        "standalone": standalone,
        "auto": auto,
        "opencode_dir": opencode_dir,
        "model": model,
        "monitor_cwd": monitor_cwd,
        "check_plugins": check_plugins,
    }
    _validate_options(name, options)

    cwd_str = str(cwd)
    prefix = f"{context}: " if context else ""
    full_env = _build_env(cwd_str, env, path_prepend)
    if check_plugins:
        await assert_no_opencode_plugins(cwd_str, full_env)

    total_attempts = max_retries + 1
    result: RunResult | None = None
    for attempt in range(1, total_attempts + 1):
        if expected_repo_name is not None:
            await assert_repo_identity(cwd_str, expected_repo_name)
        harness = None
        if guard_harness_repo:
            root = Path(harness_repo_root) if harness_repo_root is not None else None
            harness = await _snapshot_harness_repo(Path(cwd_str), root)
        logger.info("%sRunning %s backend (attempt %d/%d, cwd=%s)", prefix, name, attempt, total_attempts, cwd_str)
        result = await _attempt(
            name,
            prompt,
            cwd=cwd_str,
            env=full_env,
            timeout=timeout,
            prompt_via_file=prompt_via_file,
            options=options,
            prefix=prefix,
            expected_repo_name=expected_repo_name,
            harness=harness,
        )
        if result.not_found or (not result.timed_out and result.returncode == 0):
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
    assert result is not None  # noqa: S101 - the loop runs at least once
    if result.timed_out and expected_repo_name is not None:
        try:
            await assert_repo_identity(cwd_str, expected_repo_name)
        except RepoIdentityError:
            logger.exception("%sRepo identity check failed after a timeout-kill", prefix)
    return result


def _as_sync(func: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(func(*args, **kwargs))

    return wrapper


launch_sync = _as_sync(launch)
launch_sync.__doc__ = (
    "Sync adapter for ``launch`` (same parameters, same result): runs it with ``asyncio.run``.\n\n"
    "For sync callers (I-3, I-4, I-5). Must not be called from a running event loop; await ``launch`` there."
)
