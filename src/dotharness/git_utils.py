"""Small git helpers (more async ones live in ``dotharness.repo_guard``)."""

from pathlib import Path

from dotharness.subprocess_runner import run

_GIT_TIMEOUT_SECONDS = 30.0


class GitError(RuntimeError):
    """A git command failed, e.g. the directory is not inside a git work tree."""


async def git_toplevel(path: Path) -> Path:
    """Return the git toplevel of ``path`` (``git rev-parse --show-toplevel``); raise ``GitError`` if there is none."""
    result = await run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], timeout=_GIT_TIMEOUT_SECONDS)
    if result.not_found or result.timed_out:
        raise GitError(f"could not run git in {path}: {result.stderr}")  # noqa: TRY003
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        raise GitError(f"{path} is not inside a git repository: {result.stderr.strip()}")  # noqa: TRY003
    return Path(out)
