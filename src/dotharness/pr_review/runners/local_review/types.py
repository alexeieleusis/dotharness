"""Exception and value types shared by every local-review submodule."""

from __future__ import annotations

from dataclasses import dataclass


class LocalReviewError(RuntimeError):
    """A precondition failed; the command must exit non-zero."""


class NothingToReviewError(Exception):
    """The branch has no commits beyond the base; the command exits 0."""


@dataclass(frozen=True)
class LocalReviewTarget:
    branch: str
    base_ref: str
    base_sha: str  # the merge base
    head_sha: str


class RepoMutatedError(LocalReviewError):
    """The backend changed HEAD or tracked files during the review phase (FR-9).

    Raised by `run` AFTER the manifest and index.md were written; the caller maps it
    to a non-zero exit. Nothing is reset, cleaned or restored.
    """
