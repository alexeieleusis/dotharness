import logging
import os
import signal
import subprocess
import tempfile
from pathlib import Path

XDG_DATA = Path.home() / ".local/share/dotharness"
INLINE_THRESHOLD_BYTES = 4096

logger = logging.getLogger(__name__)


class Backend:
    def __init__(
        self, backend: str, timeout: int, path_prepend: list[str], env_vars: dict[str, str], max_retries: int = 1
    ):
        if backend not in ("opencode", "claude"):
            raise ValueError(f"Unknown backend: {backend}")  # noqa: TRY003
        self.backend_name = backend
        self.timeout = timeout
        self.path_prepend = path_prepend
        self.env_vars = env_vars
        self.max_retries = max_retries

    def run(self, instructions: str, cwd: str, opencode_dir: str | None = None) -> subprocess.CompletedProcess:
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            cmd, tmp_path = self._build_command(instructions, opencode_dir)
            env = self._build_env()
            logger.info("Running backend: %s (cwd=%s)", " ".join(cmd[:4]), cwd)
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
                        "Backend exited %d\nstdout: %s\nstderr: %s",
                        proc.returncode,
                        stdout.decode("utf-8", errors="replace")[:2000],
                        stderr.decode("utf-8", errors="replace")[:2000],
                    )
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if proc is not None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.communicate()
                    self._warn_if_backend_survived()
                if attempt < total_attempts:
                    logger.warning("Backend timed out, retrying (attempt %d/%d)", attempt + 1, total_attempts)
                    continue
                raise
            finally:
                if tmp_path is not None and tmp_path.exists():
                    tmp_path.unlink()
        raise RuntimeError("run loop exhausted without returning")  # noqa: TRY003

    def _warn_if_backend_survived(self) -> None:
        """killpg only reaches processes still in the killed group; a backend that
        double-forks into its own session (common for daemonizing subprocess managers)
        escapes it entirely and can keep running against the shared working directory
        after we've moved on to the next comment. Check by command name, not pgid, since
        the whole point is to catch processes that no longer share the killed group."""
        try:
            result = subprocess.run(  # noqa: S603
                ["pgrep", "-af", self.backend_name],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            logger.warning("Could not check for surviving %s processes after timeout-kill", self.backend_name)
            return
        survivors = result.stdout.strip()
        if survivors:
            logger.warning(
                "Backend timeout-kill: %s process(es) still alive afterward (pgid kill may have missed "
                "a daemonized child):\n%s",
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

    def _build_command(self, instructions: str, opencode_dir: str | None = None) -> tuple[list[str], Path | None]:
        if len(instructions.encode("utf-8")) <= INLINE_THRESHOLD_BYTES:
            return self._cmd_for(instructions, opencode_dir), None

        tmp_dir = XDG_DATA / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, path_str = tempfile.mkstemp(suffix=".md", dir=tmp_dir, prefix="harness_")
        tmp_path = Path(path_str)
        os.close(fd)
        tmp_path.write_text(instructions, encoding="utf-8")
        prompt = f"Read {tmp_path} and follow the instructions exactly."

        return self._cmd_for(prompt, opencode_dir), tmp_path

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.path_prepend:
            env["PATH"] = ":".join(self.path_prepend) + ":" + env.get("PATH", "")
        env.update(self.env_vars)
        return env
