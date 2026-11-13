"""Helpers for classifying paths added in a Sisyphus run."""

from __future__ import annotations

from pathlib import PurePosixPath


def _validate_path(path: str) -> None:
    """Raise ``ValueError`` if ``path`` is absolute or has a ``..`` component.

    Backslashes are not translated and the empty string is valid.
    """
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"path must be repo-relative, got absolute path: {path}")  # noqa: TRY003
    if ".." in pure.parts:
        raise ValueError(f"path must not contain a '..' component: {path}")  # noqa: TRY003
