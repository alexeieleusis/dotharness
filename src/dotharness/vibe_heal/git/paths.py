"""Path helpers shared by workflows that map files to git diff keys."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _relative_to_root(candidate: Path, root: Path) -> str | None:
    """Return ``candidate`` relative to ``root`` as POSIX, or None if outside."""
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def to_repo_relative(file_path: Path, repo_root: Path) -> str:
    """Convert a path to a repo-root-relative POSIX string.

    Absolute paths are resolved (symlinks and ``..`` included) on both sides
    before comparison. Relative paths are interpreted as CWD-relative first
    (this is what ``BranchAnalyzer`` produces when run from a subdirectory).
    If the CWD-relative candidate does not exist or lies outside the repo, but
    the repo-root-relative candidate exists, the latter is used.

    Args:
        file_path: A Path that may be absolute, CWD-relative or repo-root-relative.
        repo_root: The repository working directory.

    Returns:
        Repo-root-relative path as a POSIX string (for ``DiffParser`` map lookup).
        If the path is outside the repository, a warning is logged and the
        path's POSIX form is returned unchanged.
    """
    root = repo_root.resolve()

    if file_path.is_absolute():
        result = _relative_to_root(file_path, root)
    else:
        cwd_candidate = Path.cwd() / file_path
        root_candidate = root / file_path
        cwd_result = _relative_to_root(cwd_candidate, root)
        if cwd_result is not None and (cwd_candidate.exists() or not root_candidate.exists()):
            result = cwd_result
        elif root_candidate.exists():
            result = _relative_to_root(root_candidate, root)
        else:
            result = cwd_result

    if result is None:
        logger.warning("Path %s is outside repository root %s; diff lookup will not match", file_path, root)
        return file_path.as_posix()
    return result
