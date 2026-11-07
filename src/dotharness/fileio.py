"""Shared file primitives: atomic writes and flock-based file locks (F-DUP-5, F-DUP-3)."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, *, suffix: str = ".tmp") -> None:
    """Write ``text`` to ``path``: temp file in the same directory, fsync, then ``os.replace``.

    Creates the parent directory; the temp file is removed if anything fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int | None = 2, suffix: str = ".json") -> None:
    """:func:`atomic_write_text` of ``json.dumps(data, indent=indent)`` plus a trailing newline."""
    atomic_write_text(path, json.dumps(data, indent=indent) + "\n", suffix=suffix)


class LockContended(Exception):
    """A non-blocking :func:`file_lock` found the lock held by another process."""


@contextmanager
def file_lock(path: Path, *, blocking: bool = False) -> Generator[Path]:
    """Hold an exclusive flock on ``path`` (created, with its parent directory, if absent).

    Non-blocking by default: contention raises :class:`LockContended`. ``blocking=True`` waits for the holder.
    Released on normal exit and on exceptions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockContended(str(path)) from None
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
