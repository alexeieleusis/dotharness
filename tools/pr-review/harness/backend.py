import contextlib
import logging
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from harness.repo_guard import assert_repo_identity

XDG_DATA = Path.home() / ".local/share/dotharness"

logger = logging.getLogger(__name__)


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

    def run(
        self, instructions: str, cwd: str, opencode_dir: str | None = None, context: str | None = None
    ) -> subprocess.CompletedProcess:
        prefix = f"{context}: " if context else ""
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            if self.expected_repo_name is not None:
                assert_repo_identity(Path(cwd), self.expected_repo_name)
            cmd, tmp_path = self._build_command(instructions, opencode_dir)
            env = self._build_env()
            logger.info("%sRunning backend: %s (cwd=%s)", prefix, " ".join(cmd[:4]), cwd)
            proc = None
            try:
                proc = subprocess.Popen(  # noqa: S603
                    cmd,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, stderr = proc.communicate(timeout=self.timeout)
                if proc.returncode != 0:
                    logger.error(
                        "%sBackend exited %d\nstdout: %s\nstderr: %s",
                        prefix,
                        proc.returncode,
                        stdout.decode("utf-8", errors="replace")[:2000],
                        stderr.decode("utf-8", errors="replace")[:2000],
                    )
                if self.expected_repo_name is not None:
                    assert_repo_identity(Path(cwd), self.expected_repo_name)
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if proc is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.communicate()
                    self._warn_if_backend_survived(prefix)
                if attempt < total_attempts:
                    logger.warning("%sBackend timed out, retrying (attempt %d/%d)", prefix, attempt + 1, total_attempts)
                    continue
                raise
            finally:
                tmp_path.unlink(missing_ok=True)
        raise RuntimeError("run loop exhausted without returning")  # noqa: TRY003

    def _warn_if_backend_survived(self, prefix: str) -> None:
        """killpg only reaches processes still in the killed group; a backend that
        double-forks into its own session (common for daemonizing subprocess managers)
        escapes it entirely and can keep running against the shared working directory
        after we've moved on to the next comment. Check by command name, not pgid, since
        the whole point is to catch processes that no longer share the killed group."""
        try:
            result = subprocess.run(  # noqa: S603
                ["pgrep", "-a", "-x", self.backend_name],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            logger.warning("%sCould not check for surviving %s processes after timeout-kill", prefix, self.backend_name)
            return
        survivors = result.stdout.strip()
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
            # --pure disables external plugins so a skill can't branch/worktree on its
            # own, mirroring --disable-slash-commands below.
            cmd = ["opencode", "run", "--dangerously-skip-permissions", "--pure"]
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

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.path_prepend:
            env["PATH"] = ":".join(self.path_prepend) + ":" + env.get("PATH", "")
        env.update(self.env_vars)
        return env
