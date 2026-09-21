from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from spec_prism_flow.build import agent_instructions, git_ops
from spec_prism_flow.build.errors import CommandError

DEFAULT_TIMEOUT_SECONDS = 1800
_GIT_TIMEOUT_SECONDS = 30
_KILL_GRACE_SECONDS = 5
_TMP_DIR = Path.home() / ".local/share/dotharness/tmp"


class ClaudeCommandError(CommandError):
    """Raised when the `claude` subprocess exits non-zero."""


class RepoIdentityError(RuntimeError):
    """`cwd` isn't a standalone git checkout, or its identity (toplevel + origin url)
    changed between the start and end of a backend invocation.

    Narrowed reimplementation of pr-review's harness/repo_guard.py, scoped to what
    this phase needs: an existence check (cwd is a repo, and is itself the toplevel,
    not some subdirectory of one) plus a name match. Unlike repo_guard.py's
    assert_repo_identity, the "expected" identity to match against is derived from
    `cwd` itself at the start of invoke() rather than supplied by the caller from a
    config value -- AgentConfig (spec_prism_flow/config.py) has no repo-identity
    field today and adding one is out of this phase's scope. Re-snapshotting cwd
    after the run and comparing against that pre-invoke snapshot still catches the
    class of bug repo_guard.py exists for (a backend process that silently ends up
    operating against a different repo than the one it was pointed at), just without
    new config plumbing. `expected_repo_name`, when supplied by a caller who does
    have one, is checked in addition to the self-consistency comparison. The
    harness-repo-snapshot machinery (repo_guard.py's discover_repo_root/head_sha/
    assert_repo_unchanged, which watches a *different*, hardcoded repo -- dotharness's
    own checkout -- for a rogue process writing into it) is pr-review-specific and
    not carried over.
    """


@dataclass(frozen=True)
class _RepoSnapshot:
    toplevel: Path
    origin_url: str


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _snapshot_repo_identity(cwd: Path, *, expected_repo_name: str | None) -> _RepoSnapshot:
    cwd = cwd.resolve()

    toplevel_result = _git(cwd, "rev-parse", "--show-toplevel")
    if toplevel_result.returncode != 0:
        raise RepoIdentityError(  # noqa: TRY003
            f"{cwd} is not inside a git repository: {toplevel_result.stderr.strip()}"
        )
    toplevel = Path(toplevel_result.stdout.strip()).resolve()
    if toplevel != cwd:
        raise RepoIdentityError(  # noqa: TRY003
            f"{cwd} is not itself the toplevel of its git repo (toplevel is {toplevel}) -- refusing to "
            "operate on what looks like a subdirectory of some other checkout"
        )

    origin_result = _git(cwd, "remote", "get-url", "origin")
    if origin_result.returncode != 0:
        raise RepoIdentityError(f"{cwd} has no 'origin' remote: {origin_result.stderr.strip()}")  # noqa: TRY003
    origin_url = origin_result.stdout.strip()
    if expected_repo_name is not None:
        origin_repo_name = origin_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        if origin_repo_name != expected_repo_name:
            raise RepoIdentityError(  # noqa: TRY003
                f"{cwd}'s origin ({origin_url!r}) does not match expected repo {expected_repo_name!r}"
            )

    return _RepoSnapshot(toplevel=toplevel, origin_url=origin_url)


def _assert_repo_identity_unchanged(cwd: Path, before: _RepoSnapshot, *, expected_repo_name: str | None) -> None:
    after = _snapshot_repo_identity(cwd, expected_repo_name=expected_repo_name)
    if after != before:
        raise RepoIdentityError(  # noqa: TRY003
            f"{cwd}'s git identity changed during the backend run: {before} -> {after}"
        )


class ClaudeBackend:
    """Generalizes pr-review's harness/backend.py, narrowed to just this phase's
    needs (single instructions string in, stdout string out -- no PATH/env
    overrides, no harness-repo-snapshot cross-check)."""

    def __init__(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 1,
        expected_repo_name: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.expected_repo_name = expected_repo_name

    def invoke(self, instructions: str, cwd: Path) -> str:
        before = _snapshot_repo_identity(cwd, expected_repo_name=self.expected_repo_name)

        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            tmp_path = self._write_instructions(instructions)
            cmd = self._build_command(tmp_path)
            try:
                proc = self._start_process(cmd, cwd)
                try:
                    stdout, stderr = proc.communicate(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self._kill(proc)
                    if attempt < total_attempts:
                        git_ops.discard_working_tree_changes(cwd)
                        continue
                    raise
            finally:
                tmp_path.unlink(missing_ok=True)

            if proc.returncode != 0:
                raise ClaudeCommandError(cmd, proc.returncode, stderr.decode("utf-8", errors="replace"))

            _assert_repo_identity_unchanged(cwd, before, expected_repo_name=self.expected_repo_name)
            return stdout.decode("utf-8", errors="replace")

        raise RuntimeError("invoke loop exhausted without returning")  # noqa: TRY003

    @staticmethod
    def _write_instructions(instructions: str) -> Path:
        return agent_instructions.write_instructions_file(instructions, tmp_dir=_TMP_DIR)

    @staticmethod
    def _build_command(tmp_path: Path) -> list[str]:
        prompt = agent_instructions.read_prompt(tmp_path)
        return ["claude", "--dangerously-skip-permissions", "--disable-slash-commands", "-p", prompt]

    @staticmethod
    def _start_process(cmd: list[str], cwd: Path) -> subprocess.Popen:
        """Split out from invoke() as its own seam so tests can mock just the
        claude-invoking subprocess without also intercepting the git plumbing
        subprocess.run calls (_git, above) that invoke() and its helpers make for the
        repo-identity guard."""
        return subprocess.Popen(  # noqa: S603
            cmd,
            cwd=cwd,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_KILL_GRACE_SECONDS)
