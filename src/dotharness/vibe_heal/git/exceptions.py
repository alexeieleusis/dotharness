"""Git-specific exceptions."""


class GitError(Exception):
    """Base exception for Git errors."""


class NotAGitRepositoryError(GitError):
    """Directory is not a Git repository."""


class DirtyWorkingDirectoryError(GitError):
    """Working directory has uncommitted changes."""


class GitOperationError(GitError):
    """Git operation failed."""
