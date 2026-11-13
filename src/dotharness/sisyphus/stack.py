"""Classify paths added in a Sisyphus run as source or test files.

This docstring is the single statement of the test/source rule; the other
docstrings in this module refer to it.

Paths are repo-relative POSIX strings. A path is classified as follows:

- Source extensions are ``.py``, ``.ts``, ``.tsx``, ``.js`` and ``.jsx``. The
  extension is lower-cased before the check (the one case-insensitive step).
- A path with no source extension is dropped: docs, config and assets appear
  in neither output.
- A source-extension path is a test if any directory component is ``tests``,
  ``test`` or ``__tests__``, or if its file name matches ``test_*.py``,
  ``*_test.py``, ``*.test.*`` or ``*.spec.*``. Matching is case-sensitive, and
  only paths with a source extension are eligible to be tests.
- The test check runs first, so ``foo.test.ts`` is a test, not a source.
- Every other source-extension path is a source.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

_SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx"})
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__"})
_TEST_NAME_PATTERNS = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")


def _validate_path(path: str) -> None:
    """Raise ``ValueError`` if ``path`` is absolute or has a ``..`` component.

    Part of the module rule above. Backslashes are not translated and the empty string is valid.
    """
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"path must be repo-relative, got absolute path: {path}")  # noqa: TRY003
    if ".." in pure.parts:
        raise ValueError(f"path must not contain a '..' component: {path}")  # noqa: TRY003


def _kind(path: str) -> Literal["source", "test"] | None:
    """Return ``"source"``, ``"test"`` or ``None`` (dropped) per the module docstring."""
    pure = PurePosixPath(path)
    if pure.suffix.lower() not in _SOURCE_SUFFIXES:
        return None
    if any(part in _TEST_DIR_NAMES for part in pure.parts[:-1]):
        return "test"
    if any(fnmatchcase(pure.name, pattern) for pattern in _TEST_NAME_PATTERNS):
        return "test"
    return "source"


@dataclass(frozen=True)
class Classified:
    """Sorted, deduplicated source and test paths; see the module docstring."""

    sources: tuple[str, ...]
    tests: tuple[str, ...]


def classify_paths(paths: Sequence[str]) -> Classified:
    """Split ``paths`` into sources and tests per the module docstring.

    Every path is validated first (the first invalid one raises ``ValueError``),
    then classified. Each output tuple is sorted and deduplicated.
    """
    for path in paths:
        _validate_path(path)
    sources: set[str] = set()
    tests: set[str] = set()
    for path in paths:
        kind = _kind(path)
        if kind == "source":
            sources.add(path)
        elif kind == "test":
            tests.add(path)
    return Classified(sources=tuple(sorted(sources)), tests=tuple(sorted(tests)))
