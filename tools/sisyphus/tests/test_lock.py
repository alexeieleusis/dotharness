from __future__ import annotations

from pathlib import Path

import pytest

from sisyphus.lock import LockHeld, clone_lock, lock_key


def test_second_acquire_raises_then_reacquire(tmp_path: Path) -> None:
    clone, locks = tmp_path / "clone", tmp_path / "locks"
    clone.mkdir()
    with clone_lock(clone, locks), pytest.raises(LockHeld), clone_lock(clone, locks):
        pass
    with clone_lock(clone, locks):
        pass


def test_released_after_exception(tmp_path: Path) -> None:
    clone, locks = tmp_path / "clone", tmp_path / "locks"
    clone.mkdir()
    with pytest.raises(RuntimeError), clone_lock(clone, locks):
        raise RuntimeError("boom")
    with clone_lock(clone, locks):
        pass


def test_distinct_dirs_do_not_contend(tmp_path: Path) -> None:
    a, b, locks = tmp_path / "a", tmp_path / "b", tmp_path / "locks"
    a.mkdir()
    b.mkdir()
    with clone_lock(a, locks), clone_lock(b, locks):
        pass


def test_key_is_path_only_and_resolved(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    assert lock_key(a) == lock_key(tmp_path / "a" / ".." / "a")
    assert lock_key(a) != lock_key(tmp_path / "b")
