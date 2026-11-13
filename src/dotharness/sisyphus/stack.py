"""Helpers for classifying paths added in a Sisyphus run."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

_SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx"})
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__"})
_TEST_NAME_PATTERNS = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")


def _validate_path(path: str) -> None:
    """Raise ``ValueError`` if ``path`` is absolute or has a ``..`` component.

    Backslashes are not translated and the empty string is valid.
    """
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"path must be repo-relative, got absolute path: {path}")  # noqa: TRY003
    if ".." in pure.parts:
        raise ValueError(f"path must not contain a '..' component: {path}")  # noqa: TRY003


def _kind(path: str) -> Literal["source", "test"] | None:
    """Classify a valid repo-relative POSIX path as source, test or dropped.

    Rules, in order: a non-source extension (lower-cased) gives ``None``; a
    directory component named ``tests``, ``test`` or ``__tests__`` gives
    ``"test"``; a file name matching a test pattern gives ``"test"``; anything
    else is ``"source"``. Directory and name matching is case-sensitive.
    """
    pure = PurePosixPath(path)
    if pure.suffix.lower() not in _SOURCE_SUFFIXES:
        return None
    if any(part in _TEST_DIR_NAMES for part in pure.parts[:-1]):
        return "test"
    if any(fnmatchcase(pure.name, pattern) for pattern in _TEST_NAME_PATTERNS):
        return "test"
    return "source"
