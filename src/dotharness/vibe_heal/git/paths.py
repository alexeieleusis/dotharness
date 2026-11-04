"""Path helpers shared by workflows that map files to git diff keys."""

from pathlib import Path


def to_repo_relative(file_path: Path, repo_root: Path) -> str:
    """Convert a (possibly CWD-relative) path to a repo-root-relative POSIX string.

    Handles paths from ``BranchAnalyzer`` which may be CWD-relative when running
    from a subdirectory, or already repo-relative from the repo root.

    Args:
        file_path: A Path that may be absolute, CWD-relative or repo-root-relative.
        repo_root: The repository working directory.

    Returns:
        Repo-root-relative path as a POSIX string (for ``DiffParser`` map lookup).
    """
    try:
        if file_path.is_absolute():
            return file_path.relative_to(repo_root).as_posix()
        resolved = (Path.cwd() / file_path).resolve()
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except (ValueError, TypeError):
        return file_path.as_posix()
