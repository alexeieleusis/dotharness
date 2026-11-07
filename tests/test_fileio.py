from __future__ import annotations

import json
import os
from multiprocessing import get_context
from pathlib import Path

import pytest

from dotharness.fileio import LockContended, atomic_write_json, atomic_write_text, file_lock
from dotharness.paths import DATA_DIR_TILDE, data_dir

_FORK = get_context("fork")  # flock is POSIX-only; spawn children cannot import this module


def test_data_dir_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert data_dir() == tmp_path / ".local/share/dotharness"
    assert DATA_DIR_TILDE == "~/.local/share/dotharness"


def test_atomic_write_text_creates_parents_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b.txt"
    atomic_write_text(target, "hi")
    assert target.read_text() == "hi"
    assert [p.name for p in target.parent.iterdir()] == ["b.txt"]


def test_atomic_write_json_format(tmp_path: Path) -> None:
    atomic_write_json(tmp_path / "x.json", {"k": 1}, indent=None)
    assert (tmp_path / "x.json").read_text() == '{"k": 1}\n'
    assert json.loads((tmp_path / "x.json").read_text()) == {"k": 1}


def test_atomic_write_failure_keeps_old_file(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(target, {"old": True})
    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": object()})
    assert json.loads(target.read_text()) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_atomic_write_fsyncs_before_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    atomic_write_text(tmp_path / "y.txt", "z")
    assert calls == ["fsync", "replace"]


def test_file_lock_contention_and_release(tmp_path: Path) -> None:
    lock = tmp_path / "d" / "k.lock"
    with file_lock(lock), pytest.raises(LockContended), file_lock(lock):
        pass
    with file_lock(lock):  # released
        pass
    assert (lock.stat().st_mode & 0o777) == 0o600


def _child_blocking(path: str, marker: str) -> None:
    with file_lock(Path(path), blocking=True):
        Path(marker).write_text("acquired")


def test_file_lock_blocking_waits_for_holder(tmp_path: Path) -> None:
    lock, marker = tmp_path / "k.lock", tmp_path / "m"
    with file_lock(lock):
        proc = _FORK.Process(target=_child_blocking, args=(str(lock), str(marker)))
        proc.start()
        proc.join(0.3)
        assert proc.is_alive()
        assert not marker.exists()
    proc.join(5)
    assert marker.read_text() == "acquired"
