import contextlib
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from harness.lock import acquire_lock
from harness.repo_guard import (
    RepoIdentityError,
    assert_repo_identity,
    assert_repo_unchanged,
    discover_repo_root,
    head_sha,
)

XDG_DATA = Path.home() / ".local/share/dotharness"

# How long to wait for a backend run's own process group to fully exit before
# letting the next comment's backend run start, and how often to poll while
# waiting. See Backend._wait_for_group_to_exit.
_GROUP_EXIT_WAIT_SECONDS = 30
_GROUP_EXIT_POLL_SECONDS = 1

# How often to check a running opencode backend's own OS-level cwd against the
# directory it was launched in. See _CwdDivergenceMonitor.
_CWD_MONITOR_POLL_SECONDS = 3

# Fixed (not working-dir-scoped) lock key: serializes every opencode backend
# invocation across every config/tool on this host, not just within one batch.
# See the `with lock` in Backend.run().
_OPENCODE_GLOBAL_LOCK_KEY = "opencode-global-serialize"

logger = logging.getLogger(__name__)

# The harness's own repo checkout, discovered relative to this file rather than
# hardcoded so it works regardless of where dotharness is cloned to. None if this
# file somehow isn't inside a git repo (e.g. an unusual install layout) — in that
# case the self-repo guard below is simply skipped.
_HARNESS_REPO_ROOT = discover_repo_root(Path(__file__).resolve().parent)


class OpencodePluginError(RuntimeError):
    """opencode has a plugin installed, which can act independently of the
    session isolation --standalone otherwise provides."""


class BackendCwdDivergedError(RuntimeError):
    """The backend process's own OS-level cwd no longer matched the directory it
    was launched in — seen in production as an opencode `--standalone` session
    silently operating against a different project than the one it was given.
    Its output can't be trusted, so the process is killed rather than let finish."""


def _lsof_cwd(pid: int) -> str | None:
    """The real, kernel-level cwd of `pid` (not the PWD env var, which a
    subprocess can inherit stale and which some tools trust over getcwd()).
    None if the process is gone or lsof can't be read."""
    try:
        result = subprocess.run(  # noqa: S603
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


class _CwdDivergenceMonitor(threading.Thread):
    """Watches a running backend process for exactly the failure mode seen in
    production: `subprocess.Popen(cmd, cwd=cwd, ...)` correctly chdir's the
    process, but it's later found (via `lsof`) operating out of a different
    directory — e.g. an opencode `--standalone` session apparently rerouted
    through the always-running shared background service. Kills the process
    the moment that's detected; `Backend.run()` treats that as fatal for this
    invocation rather than trusting whatever it produced."""

    def __init__(self, proc: subprocess.Popen, expected_cwd: str, prefix: str):
        super().__init__(daemon=True)
        self._proc = proc
        self._expected_cwd = str(Path(expected_cwd).resolve())
        self._prefix = prefix
        self._stop_event = threading.Event()
        self.diverged_to: str | None = None

    def run(self) -> None:
        while not self._stop_event.wait(_CWD_MONITOR_POLL_SECONDS):
            if self._proc.poll() is not None:
                return
            actual = _lsof_cwd(self._proc.pid)
            if actual is not None and actual != self._expected_cwd:
                self.diverged_to = actual
                logger.error(
                    "%sBackend process (pid %d) cwd diverged from %s to %s mid-run — killing it, "
                    "its output cannot be trusted",
                    self._prefix,
                    self._proc.pid,
                    self._expected_cwd,
                    actual,
                )
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                return

    def stop(self) -> None:
        self._stop_event.set()


def assert_no_opencode_plugins(cwd: str) -> None:
    """`--standalone` (see `_cmd_for` below) isolates concurrent review sessions
    from each other, but says nothing about a globally-installed plugin, which
    can branch/worktree or mutate git state on its own — outside anything the
    identity/HEAD checks in repo_guard.py detect, since those only watch the
    directory the backend was *told* to operate in. Raise loudly instead of
    running the opencode backend if `opencode plugin list` reports any.

    Must run with `cwd` set to the same directory the backend run itself will
    use: plugins can be configured per-project, and `opencode plugin list`
    has no `--standalone` equivalent, so an unscoped call here would both
    check the wrong project and needlessly hit the shared background service
    right before a `--standalone` run that's specifically trying to avoid it."""
    result = subprocess.run(
        ["opencode", "plugin", "list"],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise OpencodePluginError(  # noqa: TRY003
            f"could not verify opencode has no plugins installed ('opencode plugin list' exited "
            f"{result.returncode}): {result.stderr.strip()}"
        )
    if result.stdout.strip().lower() != "no plugins found":
        raise OpencodePluginError(  # noqa: TRY003
            "opencode has one or more plugins installed — refusing to run the opencode backend, "
            "since a plugin can act independently of --standalone's session isolation "
            f"('opencode plugin list' reported):\n{result.stdout.strip()}"
        )


class Backend:
    def __init__(
        self,
        backend: str,
        timeout: int,
        path_prepend: list[str],
        env_vars: dict[str, str],
        max_retries: int = 1,
        *,
        expected_repo_name: str | None = None,
    ):
        if backend not in ("opencode", "claude"):
            raise ValueError(f"Unknown backend: {backend}")  # noqa: TRY003
        self.backend_name = backend
        self.timeout = timeout
        self.path_prepend = path_prepend
        self.env_vars = env_vars
        self.max_retries = max_retries
        self.expected_repo_name = expected_repo_name
        self._opencode_plugins_checked_dirs: set[str] = set()

    def run(
        self, instructions: str, cwd: str, opencode_dir: str | None = None, context: str | None = None
    ) -> subprocess.CompletedProcess:
        prefix = f"{context}: " if context else ""
        # Serializes every opencode invocation across every config/tool on this host
        # (not just this batch): opencode's own project-directory resolution has been
        # observed to misroute under real-world concurrency (see the cwd-divergence
        # guard below), and no in-process fix has reliably closed that, so the safest
        # mitigation is making sure there's never more than one opencode process in
        # flight anywhere. No-op for the claude backend, which isn't implicated.
        lock = acquire_lock(_OPENCODE_GLOBAL_LOCK_KEY, blocking=True) if self.backend_name == "opencode" else None
        with lock if lock is not None else contextlib.nullcontext():
            return self._run_locked(instructions, cwd, opencode_dir, prefix)

    def _run_locked(
        self, instructions: str, cwd: str, opencode_dir: str | None, prefix: str
    ) -> subprocess.CompletedProcess:
        # Checked once per (instance, cwd) rather than once per call: a Backend is
        # constructed fresh per batch (one per runner invocation, e.g. one
        # `focused-review` run across many PRs/comments — see the callers in
        # harness/runners/) and every call in a batch currently shares the same cwd,
        # so this amounts to once per batch. Keyed on cwd rather than a bare bool so a
        # Backend that did span multiple directories would still get each checked,
        # since plugins are configured per-project.
        if self.backend_name == "opencode" and cwd not in self._opencode_plugins_checked_dirs:
            assert_no_opencode_plugins(cwd)
            self._opencode_plugins_checked_dirs.add(cwd)
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            self._check_repo_identity(cwd)
            harness_head = self._snapshot_harness_repo(cwd)
            cmd, tmp_path = self._build_command(instructions, opencode_dir)
            env = self._build_env(cwd)
            logger.info("%sRunning backend: %s (cwd=%s)", prefix, " ".join(cmd[:4]), cwd)
            try:
                return self._execute(cmd, cwd, env, harness_head, prefix)
            except subprocess.TimeoutExpired:
                if attempt < total_attempts:
                    logger.warning("%sBackend timed out, retrying (attempt %d/%d)", prefix, attempt + 1, total_attempts)
                    continue
                try:
                    self._check_repo_identity(cwd)
                except RepoIdentityError:
                    logger.exception("%sRepo identity check failed after a timeout-kill", prefix)
                raise
            finally:
                tmp_path.unlink(missing_ok=True)
        raise RuntimeError("run loop exhausted without returning")  # noqa: TRY003

    def _execute(
        self, cmd: list[str], cwd: str, env: dict[str, str], harness_head: str | None, prefix: str
    ) -> subprocess.CompletedProcess:
        """Runs one attempt: spawn, watch, and validate a single backend process.
        Raises subprocess.TimeoutExpired (handled by the retry loop in
        _run_locked) or BackendCwdDivergedError (not retried — see that class)."""
        proc = None
        monitor = None
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=cwd,
                env=env,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if self.backend_name == "opencode":
                monitor = _CwdDivergenceMonitor(proc, cwd, prefix)
                monitor.start()
            stdout, stderr = proc.communicate(timeout=self.timeout)
            if monitor is not None and monitor.diverged_to is not None:
                raise BackendCwdDivergedError(  # noqa: TRY003
                    f"{prefix}backend process (pid {proc.pid}) cwd diverged from {cwd} to "
                    f"{monitor.diverged_to} mid-run and was killed; its output is untrusted"
                )
            if proc.returncode != 0:
                logger.error(
                    "%sBackend exited %d\nstdout: %s\nstderr: %s",
                    prefix,
                    proc.returncode,
                    stdout.decode("utf-8", errors="replace")[:2000],
                    stderr.decode("utf-8", errors="replace")[:2000],
                )
            self._wait_for_group_to_exit(proc.pid, prefix)
            self._check_repo_identity(cwd)
            self._assert_harness_repo_unchanged(harness_head)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.communicate()
                self._warn_if_backend_survived(prefix)
            raise
        finally:
            if monitor is not None:
                monitor.stop()

    def _check_repo_identity(self, cwd: str) -> None:
        if self.expected_repo_name is not None:
            assert_repo_identity(Path(cwd), self.expected_repo_name)

    def _snapshot_harness_repo(self, cwd: str) -> str | None:
        """None when there's nothing to watch: no harness repo was discovered, or
        `cwd` already *is* the harness repo, in which case this run is expected to
        commit into it."""
        if _HARNESS_REPO_ROOT is None or Path(cwd).resolve() == _HARNESS_REPO_ROOT:
            return None
        return head_sha(_HARNESS_REPO_ROOT)

    def _assert_harness_repo_unchanged(self, harness_head: str | None) -> None:
        if harness_head is not None:
            assert _HARNESS_REPO_ROOT is not None  # noqa: S101 — implied by harness_head being set
            assert_repo_unchanged(_HARNESS_REPO_ROOT, harness_head)

    def _wait_for_group_to_exit(self, pgid: int, prefix: str) -> None:
        """`communicate()` only waits for the top-level backend process; a server
        child it spawned (e.g. opencode's private --standalone server) can still be
        tearing down when that returns. The next comment's `run()` starts its own
        backend right after this one returns, so without this wait the two servers'
        startup/teardown can overlap — a plausible way for a request to land on the
        wrong one and answer against the wrong project directory. Bounded, since a
        member that never exits shouldn't block the batch forever."""
        deadline = time.monotonic() + _GROUP_EXIT_WAIT_SECONDS
        while True:
            survivors = self._processes_in_group(pgid)
            if not survivors:
                return
            if time.monotonic() >= deadline:
                logger.warning(
                    "%sBackend: process group %d still has member(s) %ds after this run finished, "
                    "proceeding to the next one anyway:\n%s",
                    prefix,
                    pgid,
                    _GROUP_EXIT_WAIT_SECONDS,
                    survivors,
                )
                return
            time.sleep(_GROUP_EXIT_POLL_SECONDS)

    @staticmethod
    def _pgrep(*args: str) -> str:
        """Runs `pgrep -a <args>` and returns stripped stdout. Raises on failure —
        callers decide how to log/handle that, since they differ (poll-and-retry vs.
        one-shot warning)."""
        result = subprocess.run(  # noqa: S603
            ["pgrep", "-a", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    @classmethod
    def _processes_in_group(cls, pgid: int) -> str:
        try:
            return cls._pgrep("-g", str(pgid))
        except Exception:
            logger.warning("Could not check process group %d for survivors", pgid)
            return ""

    def _warn_if_backend_survived(self, prefix: str) -> None:
        """killpg only reaches processes still in the killed group; a backend that
        double-forks into its own session (common for daemonizing subprocess managers)
        escapes it entirely and can keep running against the shared working directory
        after we've moved on to the next comment. Check by command name, not pgid, since
        the whole point is to catch processes that no longer share the killed group."""
        try:
            survivors = self._pgrep("-x", self.backend_name)
        except Exception:
            logger.warning("%sCould not check for surviving %s processes after timeout-kill", prefix, self.backend_name)
            return
        if survivors:
            logger.warning(
                "%sBackend timeout-kill: %s process(es) still alive afterward (pgid kill may have missed "
                "a daemonized child):\n%s",
                prefix,
                self.backend_name,
                survivors,
            )

    def _cmd_for(self, text: str, opencode_dir: str | None = None) -> list[str]:
        if self.backend_name == "opencode":
            # opencode v2 dropped --pure and --dangerously-skip-permissions; --standalone
            # isolates concurrent sessions but not plugins, so run() gates on
            # assert_no_opencode_plugins() above. See docs/commands/self-review.md#security.
            # --auto ("auto-approve permissions not explicitly denied") is v2's actual
            # successor to --dangerously-skip-permissions; the migration originally
            # assumed non-interactive `run` auto-approves without it, which held for
            # edit/bash but not necessarily for external_directory permission prompts
            # (opencode.json defaults those to "ask") — a headless run hitting one of
            # those with no --auto is a plausible contributor to the project-misrouting
            # incidents _CwdDivergenceMonitor above guards against.
            cmd = ["opencode", "run", "--standalone", "--auto"]
            if opencode_dir:
                cmd += ["--dir", opencode_dir]
            cmd.append(text)
            return cmd
        return ["claude", "--dangerously-skip-permissions", "--disable-slash-commands", "-p", text]

    def _build_command(self, instructions: str, opencode_dir: str | None = None) -> tuple[list[str], Path]:
        tmp_dir = XDG_DATA / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, path_str = tempfile.mkstemp(suffix=".md", dir=tmp_dir, prefix="harness_")
        tmp_path = Path(path_str)
        os.close(fd)
        try:
            tmp_path.write_text(instructions, encoding="utf-8")
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        prompt = f"Read {tmp_path} and follow the instructions exactly."

        return self._cmd_for(prompt, opencode_dir), tmp_path

    def _build_env(self, cwd: str) -> dict[str, str]:
        env = os.environ.copy()
        if self.path_prepend:
            env["PATH"] = ":".join(self.path_prepend) + ":" + env.get("PATH", "")
        env.update(self.env_vars)
        # Root cause of the opencode `--standalone` project-misrouting incidents (see
        # docs/commands/self-review.md): PWD here is otherwise whatever the harness
        # process itself inherited — e.g. from a wrapper script's `cd` before it ever
        # invoked `uv run harness run` — and can silently disagree with the directory
        # Popen actually chdir's the child into below. opencode v2 was confirmed (by
        # reproducing it directly) to trust a stale PWD over its own getcwd() and
        # re-chdir itself to match it, landing every tool call in the wrong project.
        env["PWD"] = cwd
        return env
