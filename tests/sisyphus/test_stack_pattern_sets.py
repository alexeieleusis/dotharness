"""Tests for the pattern-set listing, ``StackGroup`` and ``merge_evidence`` in ``sisyphus/stack.py``."""

from __future__ import annotations

import dataclasses
import importlib.resources
from pathlib import Path

import pytest

from dotharness.sisyphus.stack import StackError, StackGroup, merge_evidence, pattern_set_names


def _point(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("dotharness.paths.default_knowledge_dir", lambda: root)


def _make_set(root: Path, name: str, *, guide: bool = True) -> None:
    directory = root / "patterns" / name
    directory.mkdir(parents=True)
    if guide:
        (directory / "QUICK_START_MIGRATION.md").write_text("# guide\n")


def test_packaged_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    _point(monkeypatch, Path(str(importlib.resources.files("dotharness") / "knowledge")))
    assert pattern_set_names() == ("fastapi", "generic", "react")
    assert pattern_set_names() == pattern_set_names()


def test_set_without_guide_not_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_set(tmp_path, "generic")
    _make_set(tmp_path, "empty", guide=False)
    _point(monkeypatch, tmp_path)
    assert pattern_set_names() == ("generic",)


def test_missing_generic_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_set(tmp_path, "react")
    _point(monkeypatch, tmp_path)
    with pytest.raises(StackError, match=r"^knowledge/patterns/generic is missing$"):
        pattern_set_names()


def test_generic_without_guide_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_set(tmp_path, "generic", guide=False)
    _point(monkeypatch, tmp_path)
    with pytest.raises(StackError, match=r"^knowledge/patterns/generic is missing$"):
        pattern_set_names()


def test_missing_patterns_dir_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _point(monkeypatch, tmp_path)
    with pytest.raises(StackError, match=r"^knowledge/patterns/generic is missing$"):
        pattern_set_names()


def test_plain_file_ignored_and_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("zeta", "generic", "alpha"):
        _make_set(tmp_path, name)
    (tmp_path / "patterns" / "notes.md").write_text("x")
    _point(monkeypatch, tmp_path)
    assert pattern_set_names() == ("alpha", "generic", "zeta")


def test_stack_error_is_value_error() -> None:
    assert issubclass(StackError, ValueError)


def test_stack_group_fields_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(StackGroup)] == ["set", "files", "tests", "evidence", "fallback"]
    group = StackGroup("generic", ("a.py",), (), (), True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        group.set = "react"  # ty: ignore[invalid-assignment]


def test_merge_evidence() -> None:
    assert merge_evidence([("a: x", "b: y"), ("a: x", "c: z")]) == ("a: x", "b: y", "c: z")
    assert merge_evidence([]) == ()
    assert merge_evidence([(), ()]) == ()
    assert merge_evidence([("a", "A", "a ")]) == ("a", "A", "a ")


def test_merge_evidence_does_not_mutate() -> None:
    data = [["b", "a"], ["b"]]
    merge_evidence(data)
    assert data == [["b", "a"], ["b"]]
