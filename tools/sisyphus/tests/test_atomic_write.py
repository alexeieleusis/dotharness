from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sisyphus.runrecord import atomic_write_json


def test_writes_json_creating_parents_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b.json"
    atomic_write_json(target, {"k": 1})
    assert json.loads(target.read_text()) == {"k": 1}
    assert [p.name for p in target.parent.iterdir()] == ["b.json"]


def test_failure_keeps_old_file_and_removes_temp(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(target, {"old": True})
    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": object()})
    assert json.loads(target.read_text()) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_fsyncs_before_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    atomic_write_json(tmp_path / "y.json", [1])
    assert calls == ["fsync", "replace"]
