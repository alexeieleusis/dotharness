"""Tests for ``group_added_files`` in ``sisyphus/stack.py`` (match_file is stubbed)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from dotharness.sisyphus import stack
from dotharness.sisyphus.stack import FileMatch, StackError, group_added_files

NAMES = ("fastapi", "generic", "react")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(root: Path, rel_path: str, set_names: object) -> FileMatch:
        seen.append(rel_path)
        if rel_path.startswith("api/"):
            return FileMatch("fastapi", (f"{rel_path}: from fastapi import x", "pyproject.toml: fastapi"), False)
        if rel_path.endswith(".tsx"):
            return FileMatch("react", (f"{rel_path}: import react", "package.json: react"), False)
        return FileMatch("generic", (), True)

    monkeypatch.setattr(stack, "match_file", fake)
    return seen


def run(added: list[str], pattern_set: str | None = None) -> tuple[stack.StackGroup, ...]:
    return group_added_files(Path("."), added, set_names=NAMES, pattern_set=pattern_set)


def test_split_without_overlap(calls: list[str]) -> None:
    groups = run(["api/a.py", "util.py"])
    assert [(g.set, g.files) for g in groups] == [("fastapi", ("api/a.py",)), ("generic", ("util.py",))]


def test_react_group(calls: list[str]) -> None:
    groups = run(["components/Foo.tsx"])
    assert [g.set for g in groups] == ["react"]


def test_nearest_manifest_generic(calls: list[str]) -> None:
    assert [g.set for g in run(["web/inner/x.ts"])] == ["generic"]


def test_override_fastapi(calls: list[str]) -> None:
    groups = run(["api/a.py", "b.ts", "tests/x.py", "foo.test.ts", "README.md"], pattern_set="fastapi")
    assert len(groups) == 1
    g = groups[0]
    assert g.set == "fastapi" and g.fallback is False and g.evidence == ()
    assert g.files == ("api/a.py", "b.ts")
    assert g.tests == ("foo.test.ts", "tests/x.py")
    assert calls == []


def test_override_generic(calls: list[str]) -> None:
    (g,) = run(["api/a.py"], pattern_set="generic")
    assert g.set == "generic" and g.fallback is False


def test_override_unknown(calls: list[str]) -> None:
    with pytest.raises(StackError) as exc:
        run(["a.py"], pattern_set="nope")
    assert "nope" in str(exc.value)
    assert "fastapi, generic, react" in str(exc.value)
    assert calls == []


def test_tests_and_docs_excluded(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack, "nearest_manifest_dir", lambda root, rel: None)
    groups = run(["foo.test.ts", "tests/x.py", "README.md", "pyproject.toml", "a.py"])
    assert [g.files for g in groups] == [("a.py",)]
    assert groups[0].tests == ("foo.test.ts", "tests/x.py")


def test_docs_only_and_tests_only(calls: list[str]) -> None:
    assert run(["README.md", "pyproject.toml"]) == ()
    assert run(["tests/x.py"]) == ()


def test_override_without_sources(calls: list[str]) -> None:
    assert run(["tests/x.py"], pattern_set="fastapi") == ()
    assert run(["README.md"], pattern_set="nope") == ()


def test_order_and_fallback(calls: list[str]) -> None:
    groups = run(["z.py", "c/B.tsx", "api/a.py"])
    assert [g.set for g in groups] == ["fastapi", "react", "generic"]
    assert [g.fallback for g in groups] == [False, False, True]


def test_evidence_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = {"api/a.py": ("m: x", "a: 1"), "api/b.py": ("m: x", "b: 2")}
    monkeypatch.setattr(stack, "match_file", lambda r, p, s: FileMatch("fastapi", ev[p], False))
    (g,) = run(["api/b.py", "api/a.py"])
    assert g.evidence == ("m: x", "a: 1", "b: 2")


def test_determinism(calls: list[str]) -> None:
    added = ["api/a.py", "api/b.py", "x.py", "c/D.tsx", "x.py"]
    first = run(added)
    shuffled = added[:]
    random.Random(1).shuffle(shuffled)  # noqa: S311
    assert run(shuffled) == first
    assert run(added) == first


def test_match_file_called_once_per_source(calls: list[str]) -> None:
    run(["x.py", "x.py", "a.py"])
    assert calls == ["a.py", "x.py"]


def test_default_set_names(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(stack, "pattern_set_names", lambda: ("generic", "react"))
    with pytest.raises(StackError):
        group_added_files(Path("."), ["a.py"], pattern_set="fastapi")
