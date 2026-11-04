"""Shared helpers for selecting branch-modified files.

Used by the ``cleanup``, ``cleanup-pr``, and ``review`` orchestrators so that
"modified files vs. the base branch, optionally glob-filtered" has a single
implementation (``Path.match`` semantics, dim logging included).
"""

from pathlib import Path

from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.output import dim


def filter_files_by_patterns(files: list[Path], patterns: list[str]) -> list[Path]:
    """Filter files by glob patterns using ``Path.match``.

    Args:
        files: List of file paths.
        patterns: List of glob patterns (e.g. ["*.py", "src/**/*.ts"]).

    Returns:
        Files matching at least one pattern.
    """
    filtered: list[Path] = []
    for file_path in files:
        for pattern in patterns:
            if file_path.match(pattern):
                filtered.append(file_path)
                break
    return filtered


def select_modified_files(
    branch_analyzer: BranchAnalyzer,
    base_branch: str,
    file_patterns: list[str] | None = None,
) -> list[Path]:
    """Get the branch's modified files vs. ``base_branch``, optionally pattern-filtered.

    Logs the analysis start, the modified-file count, any applied filter, and the
    final list of files to process.

    Args:
        branch_analyzer: Analyzer for the repository's current branch.
        base_branch: Base branch to compare against.
        file_patterns: Optional glob patterns to filter files.

    Returns:
        The selected modified files (empty when nothing is in scope).
    """
    dim(f"Analyzing branch against {base_branch}...")
    modified_files = branch_analyzer.get_modified_files(base_branch)
    dim(f"Found {len(modified_files)} modified files")

    if not modified_files:
        return []

    if file_patterns:
        dim(f"Filtering files with patterns: {file_patterns}")
        modified_files = filter_files_by_patterns(modified_files, file_patterns)
        dim(f"After filtering: {len(modified_files)} files remain")

    if modified_files:
        dim("Files to process:")
        for f in modified_files:
            dim(f"  - {f}")

    return modified_files
