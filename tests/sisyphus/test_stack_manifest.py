"""Tests for the manifest readers in ``dotharness.sisyphus.stack``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dotharness.sisyphus.stack import (
    Declaration,
    nearest_manifest_dir,
    package_declaration,
    python_declaration,
)


def _w(root: Path, rel: str, text: str | bytes = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


NONE = Declaration(None, ())


def test_nearest_root_and_nested(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml")
    _w(tmp_path, "app/x.py")
    assert nearest_manifest_dir(tmp_path, "app/x.py") == ""
    assert nearest_manifest_dir(tmp_path, "x.py") == ""
    _w(tmp_path, "app/package.json", "{}")
    assert nearest_manifest_dir(tmp_path, "app/x.py") == "app"


def test_nearest_none(tmp_path: Path) -> None:
    _w(tmp_path, "a/b/x.py")
    assert nearest_manifest_dir(tmp_path, "a/b/x.py") is None
    assert nearest_manifest_dir(tmp_path, "missing/dir/x.py") is None


def test_nearest_skips_readme_dirs(tmp_path: Path) -> None:
    _w(tmp_path, "package.json", "{}")
    _w(tmp_path, "a/README.md")
    assert nearest_manifest_dir(tmp_path, "a/x.py") == ""


def test_nearest_requirements_variants(tmp_path: Path) -> None:
    _w(tmp_path, "a/requirements-dev.txt")
    _w(tmp_path, "b/requirements.md")
    assert nearest_manifest_dir(tmp_path, "a/x.py") == "a"
    assert nearest_manifest_dir(tmp_path, "b/x.py") is None


def test_nearest_directory_named_like_manifest(tmp_path: Path) -> None:
    (tmp_path / "a" / "requirements.txt").mkdir(parents=True)
    (tmp_path / "a" / "package.json").mkdir()
    assert nearest_manifest_dir(tmp_path, "a/x.py") is None


def test_nearest_case_sensitive(tmp_path: Path) -> None:
    _w(tmp_path, "a/Package.json")
    assert nearest_manifest_dir(tmp_path, "a/x.py") is None


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod ineffective",
)
def test_nearest_oserror_continues_upward(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml")
    locked = tmp_path / "a"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert nearest_manifest_dir(tmp_path, "a/x.py") == ""
    finally:
        locked.chmod(0o755)


def test_py_project_dependencies(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", '[project]\ndependencies = ["fastapi>=0.100"]\n')
    assert python_declaration(tmp_path, "") == Declaration("pyproject.toml: project.dependencies: fastapi>=0.100", ())


def test_py_optional_dependencies(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", '[project.optional-dependencies]\nweb = ["fastapi"]\n')
    assert python_declaration(tmp_path, "").evidence == ("pyproject.toml: project.optional-dependencies.web: fastapi")


def test_py_optional_extras_sorted(tmp_path: Path) -> None:
    _w(
        tmp_path,
        "pyproject.toml",
        '[project.optional-dependencies]\nb = ["fastapi"]\na = ["fastapi"]\n',
    )
    assert python_declaration(tmp_path, "").evidence == ("pyproject.toml: project.optional-dependencies.a: fastapi")


def test_py_poetry(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.11"\nfastapi = "*"\n')
    assert python_declaration(tmp_path, "").evidence == ("pyproject.toml: tool.poetry.dependencies: fastapi")
    _w(tmp_path, "pyproject.toml", '[tool.poetry.dependencies]\nFastAPI = "*"\n')
    assert python_declaration(tmp_path, "").evidence == ("pyproject.toml: tool.poetry.dependencies: FastAPI")


def test_py_requirements(tmp_path: Path) -> None:
    _w(tmp_path, "requirements.txt", "requests\nfastapi==0.110\n")
    assert python_declaration(tmp_path, "") == Declaration("requirements.txt: fastapi==0.110", ())


def test_py_requirements_dev_and_inline_comment(tmp_path: Path) -> None:
    _w(tmp_path, "requirements-dev.txt", "  fastapi==0.110  # api  \n")
    assert python_declaration(tmp_path, "").evidence == ("requirements-dev.txt: fastapi==0.110  # api")


@pytest.mark.parametrize(
    ("line", "declared"),
    [
        ("fastapi[all]>=0.100", True),
        ("fastapi-utils", False),
        ("FastAPI_Utils", False),
        ("# fastapi", False),
        ("-r fastapi.txt", False),
        ("FastAPI", True),
        ('fastapi ; python_version>"3.8"', True),
        ("git+https://example.com/fastapi.git", False),
    ],
)
def test_py_name_matching(tmp_path: Path, line: str, declared: bool) -> None:
    _w(tmp_path, "requirements.txt", line + "\n")
    assert (python_declaration(tmp_path, "").evidence is not None) is declared


def test_py_manifest_dir_prefix(tmp_path: Path) -> None:
    _w(tmp_path, "svc/pyproject.toml", '[project]\ndependencies = ["fastapi"]\n')
    evidence = python_declaration(tmp_path, "svc").evidence
    assert evidence is not None
    assert evidence.startswith("svc/pyproject.toml: ")
    _w(tmp_path, "svc2/requirements.txt", "fastapi\n")
    assert python_declaration(tmp_path, "svc2").evidence == "svc2/requirements.txt: fastapi"


def test_py_pyproject_wins_and_stops(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", '[project]\ndependencies = ["fastapi"]\n')
    _w(tmp_path, "requirements.txt", b"\xff\xfe")
    result = python_declaration(tmp_path, "")
    assert result.evidence is not None
    assert result.evidence.startswith("pyproject.toml: ")
    assert result.errors == ()


def test_py_bad_pyproject_then_requirements(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", "= bad")
    _w(tmp_path, "requirements.txt", "fastapi\n")
    result = python_declaration(tmp_path, "")
    assert result.evidence == "requirements.txt: fastapi"
    assert len(result.errors) == 1
    assert result.errors[0].startswith("pyproject.toml: TOMLDecodeError:")


def test_py_bad_pyproject_alone(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", "= bad")
    result = python_declaration(tmp_path, "")
    assert result.evidence is None
    assert len(result.errors) == 1


def test_py_non_utf8_requirements(tmp_path: Path) -> None:
    _w(tmp_path, "requirements.txt", b"\xff\xfe\x00")
    result = python_declaration(tmp_path, "")
    assert result.evidence is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("requirements.txt: UnicodeDecodeError: ")


def test_py_errors_in_consultation_order(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", "= bad")
    _w(tmp_path, "requirements.txt", b"\xff")
    result = python_declaration(tmp_path, "")
    assert [e.split(":")[0] for e in result.errors] == ["pyproject.toml", "requirements.txt"]


def test_py_wrong_types_ignored(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", '[project]\ndependencies = "fastapi"\n')
    assert python_declaration(tmp_path, "") == NONE
    _w(
        tmp_path,
        "pyproject.toml",
        '[project]\ndependencies = [1, "fastapi"]\n[project.optional-dependencies]\nx = "fastapi"\n'
        "[tool.poetry]\ndependencies = []\n",
    )
    assert python_declaration(tmp_path, "").evidence == "pyproject.toml: project.dependencies: fastapi"
    _w(tmp_path, "pyproject.toml", '[tool.poetry]\ndependencies = ["fastapi"]\n')
    assert python_declaration(tmp_path, "") == NONE


def test_py_no_manifests_and_directory_requirements(tmp_path: Path) -> None:
    assert python_declaration(tmp_path, "") == NONE
    (tmp_path / "requirements.txt").mkdir()
    assert python_declaration(tmp_path, "") == NONE


def test_pkg_dependencies(tmp_path: Path) -> None:
    _w(tmp_path, "package.json", '{"dependencies": {"react": "^18"}}')
    assert package_declaration(tmp_path, "") == Declaration("package.json: dependencies: react", ())
    _w(tmp_path, "web/package.json", '{"dependencies": {"React": "1"}}')
    assert package_declaration(tmp_path, "web").evidence == "web/package.json: dependencies: React"


def test_pkg_dev_and_peer_do_not_count(tmp_path: Path) -> None:
    _w(tmp_path, "package.json", '{"devDependencies": {"react": "1"}, "peerDependencies": {"react": "1"}}')
    assert package_declaration(tmp_path, "") == NONE


def test_pkg_react_dom_is_not_react(tmp_path: Path) -> None:
    _w(tmp_path, "package.json", '{"dependencies": {"react-dom": "1"}}')
    assert package_declaration(tmp_path, "") == NONE


def test_pkg_invalid_json(tmp_path: Path) -> None:
    _w(tmp_path, "package.json", "{")
    result = package_declaration(tmp_path, "")
    assert result.evidence is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("package.json: JSONDecodeError: ")


def test_pkg_non_utf8(tmp_path: Path) -> None:
    _w(tmp_path, "package.json", b"\xff")
    assert package_declaration(tmp_path, "").errors[0].startswith("package.json: UnicodeDecodeError")


def test_pkg_missing_and_wrong_types(tmp_path: Path) -> None:
    assert package_declaration(tmp_path, "") == NONE
    _w(tmp_path, "package.json", '{"dependencies": ["react"]}')
    assert package_declaration(tmp_path, "") == NONE
    _w(tmp_path, "package.json", '["react"]')
    assert package_declaration(tmp_path, "") == NONE


def test_deterministic_and_read_only(tmp_path: Path) -> None:
    _w(tmp_path, "pyproject.toml", '[project]\ndependencies = ["fastapi"]\n')
    _w(tmp_path, "a/package.json", '{"dependencies": {"react": "1"}}')
    _w(tmp_path, "a/requirements.txt", "fastapi\n")

    def snapshot() -> list[tuple[str, int, int]]:
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in tmp_path.rglob("*"))

    before = snapshot()
    first = (
        nearest_manifest_dir(tmp_path, "a/x.py"),
        python_declaration(tmp_path, "a"),
        package_declaration(tmp_path, "a"),
    )
    second = (
        nearest_manifest_dir(tmp_path, "a/x.py"),
        python_declaration(tmp_path, "a"),
        package_declaration(tmp_path, "a"),
    )
    assert first == second
    assert snapshot() == before
