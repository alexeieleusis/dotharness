"""Git integration for vibe-heal."""

from dotharness.vibe_heal.git.diff_parser import DiffParser, DiffParserError
from dotharness.vibe_heal.git.exceptions import (
    DirtyWorkingDirectoryError,
    GitError,
    GitOperationError,
    NotAGitRepositoryError,
)
from dotharness.vibe_heal.git.manager import GitManager

__all__ = [
    "DiffParser",
    "DiffParserError",
    "DirtyWorkingDirectoryError",
    "GitError",
    "GitManager",
    "GitOperationError",
    "NotAGitRepositoryError",
]
