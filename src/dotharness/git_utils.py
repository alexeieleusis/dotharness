"""Small synchronous git helpers (the async ones live in ``dotharness.repo_guard``)."""

import subprocess
from pathlib import Path

_GIT_TIMEOUT_SECONDS = 30.0


class GitError(RuntimeError):
    """A git command failed, e.g. the directory is not inside a git work tree."""


def git_toplevel(path: Path) -> Path:
    """Return the git toplevel of ``path`` (``git rev-parse --show-toplevel``); raise ``GitError`` if there is none."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not run git in {path}: {exc}") from exc  # noqa: TRY003
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        raise GitError(f"{path} is not inside a git repository: {result.stderr.strip()}")  # noqa: TRY003
    return Path(out)
