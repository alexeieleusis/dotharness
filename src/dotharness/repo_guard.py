"""Repo-identity and harness-repo guards for backend runs.

``assert_repo_identity`` checks that a directory is a git toplevel with an ``origin`` remote and leaves the decision
"is that the expected repository" to the caller (the code host knows how to read its own remote URLs);
``snapshot_harness_repo`` / ``assert_harness_unchanged`` check this checkout's HEAD did not move during a run.
Independent of any launcher, so other callers can reuse them.
"""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from dotharness.subprocess_runner import RunResult, run

logger = logging.getLogger(__name__)

OriginMatcher = Callable[[str], Awaitable[bool]]
"""Decides whether an ``origin`` remote URL belongs to the expected repository (supplied by the code host)."""

IdentityCheck = Callable[[str | Path], Awaitable[None]]
"""Raises ``RepoIdentityError`` unless the directory is the expected repository's clone."""

_HELPER_TIMEOUT_SECONDS = 30.0


class RepoIdentityError(RuntimeError):
    """``cwd`` is not the expected git repo, or the harness repo's HEAD moved."""


async def _git(working_dir: Path, *args: str) -> RunResult:
    return await run(["git", "-C", str(working_dir), *args], timeout=_HELPER_TIMEOUT_SECONDS)


async def assert_repo_identity(
    working_dir: str | Path, origin_matches: OriginMatcher, expected: str = "the expected repository"
) -> None:
    """Raise ``RepoIdentityError`` unless ``working_dir`` is a git toplevel whose ``origin`` ``origin_matches`` accepts.

    ``expected`` only names the repository in the error message.
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
    if not await origin_matches(origin.stdout.strip()):
        msg = f"{directory}'s origin does not match the expected repo {expected!r}"
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


async def snapshot_harness_repo(cwd: Path, root: Path | None) -> tuple[Path, str] | None:
    """(root, HEAD) to watch, or None when there is nothing to watch (no repo found, or ``cwd`` is that repo)."""
    if root is None:
        root = await _discover_repo_root(Path(__file__).resolve().parent)
    if root is None or cwd.resolve() == root.resolve():
        return None
    return root, await _head_sha(root)


async def assert_harness_unchanged(snapshot: tuple[Path, str] | None) -> None:
    if snapshot is None:
        return
    root, head = snapshot
    current = await _head_sha(root)
    if current != head:
        msg = f"{root}'s HEAD moved from {head} to {current} during a backend run that was not supposed to touch it"
        raise RepoIdentityError(msg)
