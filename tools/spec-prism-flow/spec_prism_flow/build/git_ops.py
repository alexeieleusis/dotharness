from __future__ import annotations

import subprocess
from pathlib import Path

from spec_prism_flow.build.errors import CommandError


class GitCommandError(CommandError):
    """A git subprocess exited non-zero."""


def _run(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitCommandError(["git", *args], result.returncode, result.stderr)
    return result


def checkout_fresh_branch(clone: Path, branch: str, base: str = "main") -> None:
    """Create `branch` off origin/`base` in `clone`, discarding any prior local branch
    of the same name -- safe to re-run (e.g. on a retried run_phase), since it always
    starts from the remote's current base, never from whatever was locally lying
    around."""
    _run(clone, "fetch", "origin", base)
    _run(clone, "checkout", "-B", branch, f"origin/{base}")


def commit_all(clone: Path, message: str) -> bool:
    """Stage and commit everything in `clone`. Returns False (no commit made) if the
    working tree was already clean -- the caller uses this to report an empty
    AgentRunResult rather than push and open an empty PR."""
    _run(clone, "add", "-A")
    status = _run(clone, "status", "--porcelain")
    if not status.stdout.strip():
        return False
    _run(clone, "commit", "-m", message, "--")
    return True


def push_branch(clone: Path, branch: str, remote: str = "origin") -> None:
    """`--force-with-lease` so a retried run_phase (which recreates `branch` fresh off
    origin/base via checkout_fresh_branch) can overwrite a stale remote branch from an
    earlier, abandoned attempt, while still protecting against clobbering an
    unexpected concurrent push."""
    _run(clone, "push", "--force-with-lease", "-u", remote, branch)


def fetch_resync(clone: Path, branch: str, remote: str = "origin") -> None:
    """Force `clone` to exactly match `remote`/`branch`."""
    _run(clone, "fetch", remote, branch)
    _run(clone, "checkout", "-B", branch, f"{remote}/{branch}")


def diff_name_only(clone: Path, base_ref: str = "origin/main") -> list[str]:
    result = _run(clone, "diff", "--name-only", f"{base_ref}...HEAD")
    return [line for line in result.stdout.splitlines() if line.strip()]


def head_sha(clone: Path) -> str:
    """Current HEAD commit sha for `clone`'s repo. Not part of the source
    orchestrate.git_ops module's ported set, but needed by agent_runner.run_phase to
    populate AgentRunResult.commit_sha after a successful commit."""
    return _run(clone, "rev-parse", "HEAD").stdout.strip()
