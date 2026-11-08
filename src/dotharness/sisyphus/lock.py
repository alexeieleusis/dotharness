"""Non-blocking per-clone lock (flock), keyed on the resolved clone directory.

Mirrors the harness idea (hash of the resolved path only) without importing harness.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import hashlib
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from dotharness.fileio import LockContended, file_lock
from dotharness.paths import DATA_DIR_TILDE

DEFAULT_LOCK_DIR = f"{DATA_DIR_TILDE}/sisyphus/locks"


class LockHeld(Exception):
    """Another process holds the lock for this clone. The CLI maps this to a quiet exit 0."""


def lock_key(clone_dir: str | os.PathLike[str]) -> str:
    """Hash of the resolved clone directory (path only)."""
    resolved = str(Path(clone_dir).expanduser().resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def lock_path(clone_dir: str | os.PathLike[str], lock_dir: str | os.PathLike[str] | None = None) -> Path:
    base = Path(lock_dir if lock_dir is not None else DEFAULT_LOCK_DIR).expanduser()
    return base / f"{lock_key(clone_dir)}.lock"


@contextmanager
def clone_lock(
    clone_dir: str | os.PathLike[str],
    lock_dir: str | os.PathLike[str] | None = None,
) -> Generator[Path]:
    """Hold the lock for ``clone_dir``; raise :class:`LockHeld` if contended.

    Released on normal exit and on exceptions. Different clone dirs never contend.
    """
    path = lock_path(clone_dir, lock_dir)
    try:
        with file_lock(path):
            yield path
    except LockContended:
        raise LockHeld(f"lock held for clone {clone_dir} ({path})") from None
