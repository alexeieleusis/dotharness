"""Tests for test-file assignment in ``group_added_files`` (match_file is stubbed, manifests are real)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from dotharness.sisyphus import stack
from dotharness.sisyphus.stack import FileMatch, group_added_files

NAMES = ("fastapi", "generic", "react")


@pytest.fixture
def sets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    mapping: dict[str, str] = {}

    def fake(root: Path, rel_path: str, set_names: object) -> FileMatch:
        name = mapping.get(rel_path, "generic")
        return FileMatch(name, (f"ev:{rel_path}",), name == "generic")

    monkeypatch.setattr(stack, "match_file", fake)
    return mapping


def manifest(root: Path, *dirs: str, name: str = "pyproject.toml") -> None:
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / name).write_text("")


def run(root: Path, added: list[str], pattern_set: str | None = None) -> dict[str, tuple[str, ...]]:
    return {g.set: g.tests for g in group_added_files(root, added, set_names=NAMES, pattern_set=pattern_set)}


def test_single_candidate(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "services/api")
    sets["services/api/app/main.py"] = "fastapi"
    got = run(tmp_path, ["services/api/app/main.py", "services/api/tests/test_main.py"])
    assert got == {"fastapi": ("services/api/tests/test_main.py",)}


@pytest.fixture
def mixed(tmp_path: Path, sets: dict[str, str]) -> Path:
    manifest(tmp_path, "services/api")
    sets["services/api/app/main.py"] = "fastapi"
    return tmp_path


MIXED = ["services/api/app/main.py", "services/api/scripts/run.py"]


def test_longest_prefix_fastapi(mixed: Path) -> None:
    got = run(mixed, [*MIXED, "services/api/app/tests/test_a.py"])
    assert got == {"fastapi": ("services/api/app/tests/test_a.py",), "generic": ()}


def test_longest_prefix_generic(mixed: Path) -> None:
    got = run(mixed, [*MIXED, "services/api/scripts/tests/test_run.py"])
    assert got == {"fastapi": (), "generic": ("services/api/scripts/tests/test_run.py",)}


def test_tie_fastapi_vs_generic(mixed: Path) -> None:
    got = run(mixed, [*MIXED, "services/api/tests/test_a.py"])
    assert got["fastapi"] == ("services/api/tests/test_a.py",) and got["generic"] == ()


def test_tie_react_vs_generic(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "web", name="package.json")
    sets["web/a.tsx"] = "react"
    got = run(tmp_path, ["web/a.tsx", "web/b.ts", "web/tests/t.ts"])
    assert got["react"] == ("web/tests/t.ts",) and got["generic"] == ()


def test_tie_fastapi_vs_react(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "app")
    manifest(tmp_path, "app", name="package.json")
    sets["app/a.py"] = "fastapi"
    sets["app/b.tsx"] = "react"
    got = run(tmp_path, ["app/a.py", "app/b.tsx", "app/tests/t.py"])
    assert got == {"fastapi": ("app/tests/t.py",), "react": ()}


def test_whole_component_prefix(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "web")
    sets["web/api2/a.py"] = "fastapi"
    got = run(tmp_path, ["web/api2/a.py", "web/api/b.py", "web/api/tests/test_b.py"])
    assert got == {"fastapi": (), "generic": ("web/api/tests/test_b.py",)}


def test_no_manifest_goes_to_generic(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "svc")
    sets["svc/a.py"] = "fastapi"
    got = run(tmp_path, ["svc/a.py", "util.py", "tests/test_u.py"])
    assert got == {"fastapi": (), "generic": ("tests/test_u.py",)}


def test_without_generic_group_test_is_omitted(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "svc")
    sets["svc/a.py"] = "fastapi"
    got = run(tmp_path, ["svc/a.py", "tests/test_u.py"])
    assert got == {"fastapi": ()}


def test_manifest_dir_without_added_source(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "svc", "other")
    sets["svc/a.py"] = "fastapi"
    got = run(tmp_path, ["svc/a.py", "util.py", "other/tests/test_o.py"])
    assert got["generic"] == ("other/tests/test_o.py",) and got["fastapi"] == ()


def test_root_manifest(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, ".")
    sets["app.py"] = "fastapi"
    got = run(tmp_path, ["app.py", "util/x.py", "test_app.py"])
    assert got["fastapi"] == ("test_app.py",) and got["generic"] == ()


def test_each_test_once_sorted_and_not_in_files(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "a", "b")
    sets["a/x.py"] = "fastapi"
    added = ["a/x.py", "b/y.py", "b/tests/z.py", "a/tests/q.py", "a/tests/b.py", "foo.test.ts", "tests/x.py"]
    groups = group_added_files(tmp_path, added, set_names=NAMES)
    union = [t for g in groups for t in g.tests]
    assert len(union) == len(set(union))
    assert set(union) <= {"b/tests/z.py", "a/tests/q.py", "a/tests/b.py", "foo.test.ts", "tests/x.py"}
    assert all(g.tests == tuple(sorted(g.tests)) for g in groups)
    assert groups[0].tests == ("a/tests/b.py", "a/tests/q.py")
    assert not {t for g in groups for t in g.files} & set(union)


def test_override_assigns_all_without_manifest_walk(
    tmp_path: Path, sets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(stack, "nearest_manifest_dir", lambda root, rel: seen.append(rel))
    got = run(tmp_path, ["a.py", "tests/x.py", "foo.test.ts"], pattern_set="fastapi")
    assert got == {"fastapi": ("foo.test.ts", "tests/x.py")}
    assert seen == []


def test_deterministic_and_other_fields_unchanged(tmp_path: Path, sets: dict[str, str]) -> None:
    manifest(tmp_path, "svc")
    sets["svc/a.py"] = "fastapi"
    added = ["svc/a.py", "svc/b.py", "svc/tests/t.py", "x.py", "tests/y.py"]
    first = group_added_files(tmp_path, added, set_names=NAMES)
    shuffled = added * 2
    random.Random(1).shuffle(shuffled)  # noqa: S311
    assert group_added_files(tmp_path, shuffled, set_names=NAMES) == first
    assert group_added_files(tmp_path, added, set_names=NAMES) == first
    assert [(g.set, g.files, g.evidence, g.fallback) for g in first] == [
        ("fastapi", ("svc/a.py",), ("ev:svc/a.py",), False),
        ("generic", ("svc/b.py", "x.py"), ("ev:svc/b.py", "ev:x.py"), True),
    ]


def test_docstring_states_rule() -> None:
    doc = stack.__doc__ or ""
    assert "nearest_manifest_dir" in doc
    assert "whole path components" in doc
    assert "``generic`` last" in doc
