"""Tests for ``match_file`` in ``dotharness.sisyphus.stack``."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from dotharness.sisyphus import stack
from dotharness.sisyphus.stack import Declaration, FileMatch, FrameworkRule, match_file

ALL = ("generic", "fastapi", "react")
FASTAPI_PY = "from fastapi import APIRouter\n"
REACT_TSX = "import React from 'react'\n"


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def py_project(root: Path, deps: str = '"fastapi"') -> None:
    write(root, "pyproject.toml", f"[project]\nname='x'\ndependencies=[{deps}]\n")
    write(root, "app/x.py", FASTAPI_PY)


def react_project(root: Path, section: str = "dependencies") -> None:
    write(root, "package.json", f'{{"{section}": {{"react": "18"}}}}')
    write(root, "components/Foo.tsx", REACT_TSX)


GENERIC = FileMatch("generic", (), True)


def test_fastapi_match(tmp_path: Path) -> None:
    py_project(tmp_path)
    assert match_file(tmp_path, "app/x.py", ALL) == FileMatch(
        "fastapi",
        ("pyproject.toml: project.dependencies: fastapi", "app/x.py: from fastapi import APIRouter"),
        False,
    )


def test_fastapi_no_dependency(tmp_path: Path) -> None:
    py_project(tmp_path, '"requests"')
    assert match_file(tmp_path, "app/x.py", ALL) == GENERIC


def test_fastapi_poetry_and_extras(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[tool.poetry.dependencies]\nfastapi='1'\n")
    write(tmp_path, "app/x.py", FASTAPI_PY)
    assert match_file(tmp_path, "app/x.py", ALL).set == "fastapi"
    write(
        tmp_path,
        "pyproject.toml",
        "[project]\nname='x'\n[project.optional-dependencies]\nweb=['fastapi']\n",
    )
    assert match_file(tmp_path, "app/x.py", ALL).set == "fastapi"


def test_requirements_and_lookalike(tmp_path: Path) -> None:
    write(tmp_path, "requirements.txt", "fastapi==0.110\n")
    write(tmp_path, "app/x.py", FASTAPI_PY)
    assert match_file(tmp_path, "app/x.py", ALL).set == "fastapi"
    write(tmp_path, "requirements.txt", "fastapi-utils\n")
    assert match_file(tmp_path, "app/x.py", ALL) == GENERIC


def test_react_match(tmp_path: Path) -> None:
    react_project(tmp_path)
    assert match_file(tmp_path, "components/Foo.tsx", ALL) == FileMatch(
        "react",
        ("package.json: dependencies: react", "components/Foo.tsx: import React from 'react'"),
        False,
    )


def test_react_ts_layouts(tmp_path: Path) -> None:
    react_project(tmp_path)
    write(tmp_path, "components/a.ts", "const x = 1\n")
    assert match_file(tmp_path, "components/a.ts", ALL) == GENERIC
    write(tmp_path, "lib/a.ts", "import React from 'react'\n")
    assert match_file(tmp_path, "lib/a.ts", ALL) == GENERIC


def test_react_dev_dependency_only(tmp_path: Path) -> None:
    react_project(tmp_path, "devDependencies")
    assert match_file(tmp_path, "components/Foo.tsx", ALL) == GENERIC


def test_unparseable_pyproject(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[[[ nope")
    write(tmp_path, "app/x.py", FASTAPI_PY)
    result = match_file(tmp_path, "app/x.py", ALL)
    assert result.set == "generic" and result.fallback
    assert len(result.evidence) == 1
    assert result.evidence[0].startswith("pyproject.toml: TOMLDecodeError: ")


def test_unparseable_pyproject_with_requirements(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[[[ nope")
    write(tmp_path, "requirements.txt", "fastapi\n")
    write(tmp_path, "app/x.py", FASTAPI_PY)
    result = match_file(tmp_path, "app/x.py", ALL)
    assert result.set == "fastapi"
    assert len(result.evidence) == 2


def test_only_nearest_manifest(tmp_path: Path) -> None:
    react_project(tmp_path)
    write(tmp_path, "inner/package.json", '{"dependencies": {}}')
    write(tmp_path, "inner/components/Foo.tsx", REACT_TSX)
    assert match_file(tmp_path, "inner/components/Foo.tsx", ALL) == GENERIC


def test_wrong_manifest_kind(tmp_path: Path) -> None:
    write(tmp_path, "package.json", '{"dependencies": {"react": "1"}}')
    write(tmp_path, "app/x.py", FASTAPI_PY)
    assert match_file(tmp_path, "app/x.py", ALL) == GENERIC
    other = tmp_path / "py"
    write(other, "pyproject.toml", "[project]\ndependencies=['fastapi']\n")
    write(other, "a/b.ts", "import React from 'react'\n")
    assert match_file(other, "a/b.ts", ALL) == GENERIC


def test_no_manifest(tmp_path: Path) -> None:
    write(tmp_path, "app/x.py", FASTAPI_PY)
    assert match_file(tmp_path, "app/x.py", ALL) == GENERIC


def test_set_names_exclude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    react_project(tmp_path)
    py_project(tmp_path / "py")

    def boom(*_a: object) -> Declaration:
        raise AssertionError("manifest read")  # noqa: TRY003

    rules = tuple(dataclasses.replace(r, read_manifest=boom) for r in stack.FRAMEWORK_RULES)
    monkeypatch.setattr(stack, "FRAMEWORK_RULES", rules)
    assert match_file(tmp_path, "components/Foo.tsx", ("generic", "fastapi")) == GENERIC
    assert match_file(tmp_path / "py", "app/x.py", ("generic", "react")) == GENERIC


def test_missing_source_file(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[project]\ndependencies=['fastapi']\n")
    result = match_file(tmp_path, "app/gone.py", ALL)
    assert result.set == "generic" and result.fallback
    assert len(result.evidence) == 1
    assert result.evidence[0].startswith("app/gone.py: FileNotFoundError: ")


def test_missing_source_without_dependency_never_opened(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[project]\ndependencies=[]\n")
    assert match_file(tmp_path, "app/gone.py", ALL) == GENERIC


def test_repeatable_frozen_and_fallback(tmp_path: Path) -> None:
    py_project(tmp_path)
    first = match_file(tmp_path, "app/x.py", ALL)
    assert match_file(tmp_path, "app/x.py", ALL) == first
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.set = "x"  # ty: ignore[invalid-assignment]
    assert first.fallback is False
    for names in (frozenset(ALL), list(ALL), tuple(ALL)):
        assert match_file(tmp_path, "app/x.py", names).fallback is False
    assert match_file(tmp_path, "app/x.py", ("generic",)).fallback is True


def test_uppercase_extension(tmp_path: Path) -> None:
    py_project(tmp_path)
    write(tmp_path, "app/Y.PY", FASTAPI_PY)
    assert match_file(tmp_path, "app/Y.PY", ALL).set == "fastapi"


def _row(name: str, ext: str, decl: Declaration, layout: str | None) -> FrameworkRule:
    return FrameworkRule(name, (ext,), lambda _r, _m: decl, lambda _p, _l: layout)


def test_monkeypatched_table_and_single_source_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, "pyproject.toml", "[project]\ndependencies=[]\n")
    write(tmp_path, "a.py", "x = 1\n")
    assert match_file(tmp_path, "a.py", ("generic", "extra", "extra2")) == GENERIC
    reads: list[str] = []
    real = stack.read_source_lines

    def spy(root: Path, rel: str) -> tuple[tuple[str, ...], str | None]:
        reads.append(rel)
        return real(root, rel)

    monkeypatch.setattr(stack, "read_source_lines", spy)
    rows = (
        _row("extra", ".py", Declaration("m: dep", ()), None),
        _row("extra2", ".py", Declaration("m: dep2", ()), "a.py: hit"),
    )
    monkeypatch.setattr(stack, "FRAMEWORK_RULES", rows)
    result = match_file(tmp_path, "a.py", ("generic", "extra", "extra2"))
    assert result == FileMatch("extra2", ("m: dep2", "a.py: hit"), False)
    assert reads == ["a.py"]


def test_source_error_reported_once_across_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, "pyproject.toml", "[project]\n")
    rows = (
        _row("one", ".py", Declaration("m: a", ()), None),
        _row("two", ".py", Declaration("m: b", ()), None),
    )
    monkeypatch.setattr(stack, "FRAMEWORK_RULES", rows)
    result = match_file(tmp_path, "gone.py", ("one", "two"))
    assert len(result.evidence) == 1


def test_skipped_rows_not_called_and_errors_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, "pyproject.toml", "[project]\n")
    write(tmp_path, "a.py", "x\n")
    calls: list[str] = []

    def reader(tag: str, decl: Declaration):
        def read(_r: Path, _m: str) -> Declaration:
            calls.append(tag)
            return decl

        return read

    err = Declaration(None, ("e1", "e2"))
    err2 = Declaration(None, ("e2", "e0"))
    rows = (
        FrameworkRule("a", (".py",), reader("a", err), lambda _p, _l: None),
        FrameworkRule("b", (".py",), reader("b", err2), lambda _p, _l: None),
        FrameworkRule("c", (".py",), reader("c", err), lambda _p, _l: None),
        FrameworkRule("d", (".js",), reader("d", err), lambda _p, _l: None),
    )
    monkeypatch.setattr(stack, "FRAMEWORK_RULES", rows)
    result = match_file(tmp_path, "a.py", ("a", "b", "d"))
    assert calls == ["a", "b"]
    assert result == FileMatch("generic", ("e1", "e2", "e0"), True)


def test_earlier_errors_dropped_on_later_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, "pyproject.toml", "[project]\n")
    write(tmp_path, "a.py", "x\n")
    rows = (
        _row("one", ".py", Declaration(None, ("boom",)), None),
        _row("two", ".py", Declaration("m: d", ()), "a.py: hit"),
    )
    monkeypatch.setattr(stack, "FRAMEWORK_RULES", rows)
    assert match_file(tmp_path, "a.py", ("one", "two")).evidence == ("m: d", "a.py: hit")
