"""Tests for the sisyphus gate-record writer."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from dotharness import fileio
from dotharness.sisyphus import gaterecord
from dotharness.sisyphus.gaterecord import write_gate_record

_GOLDEN = json.loads((Path(__file__).parents[1] / "contracts" / "gate_record_golden.json").read_text(encoding="utf-8"))
_RECORD = _GOLDEN["record"]
_ALLOWED = {"__future__", "pathlib", "dotharness.fileio", "dotharness.paths"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _state(home: Path) -> Path:
    return home / ".local/share/dotharness/state"


def test_golden_record_written(home):
    path = write_gate_record("acme-frontend", 7, _RECORD["head_sha"])
    assert json.loads(path.read_text(encoding="utf-8")) == _RECORD
    assert path == _state(home) / "acme-frontend" / "build_gate_7.json"
    case = _GOLDEN["path_cases"][0]
    assert write_gate_record(case["slug"], case["pr"], "x").as_posix().endswith(case["relative_path"])


def test_creates_parent_directory(home):
    assert not _state(home).exists()
    write_gate_record("acme-frontend", 1, "abc")
    assert (_state(home) / "acme-frontend").is_dir()


def test_overwrite_and_idempotent(home):
    path = write_gate_record("acme-frontend", 1, "old")
    write_gate_record("acme-frontend", 1, "new")
    assert json.loads(path.read_text(encoding="utf-8")) == {"head_sha": "new"}
    write_gate_record("acme-frontend", 1, "new")
    first = path.read_bytes()
    write_gate_record("acme-frontend", 1, "new")
    assert path.read_bytes() == first
    assert [p.name for p in path.parent.iterdir()] == ["build_gate_1.json"]


@pytest.mark.parametrize("slug", _GOLDEN["invalid_slugs"])
def test_invalid_slug_raises_and_writes_nothing(slug, home):
    with pytest.raises(ValueError, match="Invalid repo slug"):
        write_gate_record(slug, 1, "abc")
    assert not (home / ".local").exists()


def test_oserror_propagates(home, monkeypatch):
    def boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(gaterecord, "atomic_write_json", boom)
    with pytest.raises(PermissionError, match="denied"):
        write_gate_record("acme-frontend", 1, "abc")


@pytest.mark.parametrize("target", ["replace", "fsync"])
def test_failed_write_leaves_no_partial_file(target, home, monkeypatch):
    path = write_gate_record("acme-frontend", 1, "good")
    before = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(fileio.os, target, boom)
    with pytest.raises(OSError, match="disk"):
        write_gate_record("acme-frontend", 1, "bad")
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ["build_gate_1.json"]


def test_failed_first_write_leaves_no_file(home, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk"):
        write_gate_record("acme-frontend", 2, "bad")
    assert list((_state(home) / "acme-frontend").iterdir()) == []


def test_gaterecord_imports_only_allowed_modules():
    tree = ast.parse(Path(gaterecord.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert modules <= _ALLOWED
