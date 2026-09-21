from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from spec_prism_flow.build.errors import CommandError

_GIT_TIMEOUT_SECONDS = 30


class GitCommandError(CommandError):
    """A git subprocess exited non-zero."""


def _run(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=clone,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(
            ["git", *args], -1, f"timed out after {_GIT_TIMEOUT_SECONDS}s: {exc.stderr or ''}"
        ) from exc
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
    unexpected concurrent push.

    `checkout_fresh_branch` never fetches `origin/<branch>`, so a clone with no
    remote-tracking ref for it (e.g. a --single-branch clone, or any clone made
    before an earlier attempt's push) would make bare `--force-with-lease` expect
    the ref to be absent on the remote -- and reject the push with a stale-info
    error exactly when an abandoned branch is actually there. Fetching `branch`
    first (tolerating it not existing yet) gives the lease fresh remote state to
    evaluate against."""
    with contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603
            ["git", "fetch", remote, branch],  # noqa: S607
            cwd=clone,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    _run(clone, "push", "--force-with-lease", "-u", remote, branch)


def delete_remote_branch_if_exists(clone: Path, branch: str, remote: str = "origin") -> None:
    """Deletes `remote`/`branch` if it exists. Used when a retried run_phase comes back
    empty, so a stale branch (and any PR built from it) left behind by an earlier,
    non-empty attempt doesn't keep pointing at an abandoned commit -- mirrors
    checkout_fresh_branch's "always reset to a known-good state" philosophy on the
    remote side too."""
    result = subprocess.run(  # noqa: S603
        ["git", "ls-remote", "--exit-code", "--heads", remote, branch],  # noqa: S607
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 2:
        return
    if result.returncode != 0:
        raise GitCommandError(
            ["git", "ls-remote", "--exit-code", "--heads", remote, branch], result.returncode, result.stderr
        )
    _run(clone, "push", remote, "--delete", branch)


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


def discard_working_tree_changes(clone: Path) -> None:
    """Discard uncommitted tracked edits (staged or not) and untracked files in
    `clone`, restoring it to its current branch tip. Not part of the source
    orchestrate.git_ops module's ported set; used by ClaudeBackend to recover from a
    SIGKILLed attempt that left partially written/staged files before a retry.

    `reset --hard` (not `checkout -- .`) so a partially staged change from the
    timed-out attempt is dropped from the index too, not just the worktree --
    otherwise it would survive this cleanup and the retry's `commit_all` would
    commit it as if it were new work."""
    _run(clone, "reset", "--hard", "HEAD")
    _run(clone, "clean", "-fd")
