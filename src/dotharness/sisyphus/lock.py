"""Non-blocking per-clone lock (flock), keyed on the resolved clone directory.

Mirrors the harness idea (hash of the resolved path only) without importing harness.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LOCK_DIR = "~/.local/share/dotharness/sisyphus/locks"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeld(f"lock held for clone {clone_dir} ({path})") from None
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
