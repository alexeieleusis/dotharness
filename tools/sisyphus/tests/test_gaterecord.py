from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from sisyphus import gaterecord
from sisyphus.gaterecord import (
    fallback_repo_slug,
    gate_record_path,
    read_gate_record,
    repo_slug_from_name,
    validate_repo_slug,
    write_gate_record,
)

SHA = "a" * 40


def test_default_path_format_is_pinned_to_merge_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert gate_record_path("Oscilar-backend", 7) == (
        tmp_path / ".local/share/dotharness/state/Oscilar-backend/build_gate_7.json"
    )


def test_state_dir_replaces_state_root(tmp_path: Path) -> None:
    assert gate_record_path("o-r", 3, tmp_path) == tmp_path / "o-r" / "build_gate_3.json"


def test_path_creates_nothing(tmp_path: Path) -> None:
    gate_record_path("o-r", 3, tmp_path / "state")
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("slug", ["owner/repo", "", "a b", "../x"])
def test_invalid_slug_rejected(slug: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid repo slug"):
        gate_record_path(slug, 1, tmp_path)


def test_slug_is_hyphenated_owner_repo() -> None:
    assert repo_slug_from_name("Oscilar/backend") == "Oscilar-backend"


def test_fallback_slug_shape(tmp_path: Path) -> None:
    clone = tmp_path / "My Clone!"
    clone.mkdir()
    slug = fallback_repo_slug(clone)
    assert slug.startswith("My-Clone-")
    assert len(slug.rsplit("-", 1)[1]) == 8


def test_write_content_and_roundtrip(tmp_path: Path) -> None:
    path = write_gate_record("o-r", 5, SHA, tmp_path, commands=["make check", "make test"])
    assert path == tmp_path / "o-r" / "build_gate_5.json"
    data = json.loads(path.read_text())
    assert data["head_sha"] == SHA
    assert data["commands"] == ["make check", "make test"]
    assert data["passed_at"].endswith("+00:00")
    assert read_gate_record("o-r", 5, tmp_path) == SHA


def test_write_without_commands_has_no_commands_key(tmp_path: Path) -> None:
    data = json.loads(write_gate_record("o-r", 5, SHA, tmp_path).read_text())
    assert "commands" not in data


def test_write_replaces_old_record_and_leaves_no_temp_files(tmp_path: Path) -> None:
    write_gate_record("o-r", 5, SHA, tmp_path)
    write_gate_record("o-r", 5, "b" * 40, tmp_path)
    assert read_gate_record("o-r", 5, tmp_path) == "b" * 40
    assert [p.name for p in (tmp_path / "o-r").iterdir()] == ["build_gate_5.json"]


def test_write_is_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_gate_record("o-r", 5, SHA, tmp_path)

    def boom(src: object, dst: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr(gaterecord.os, "replace", boom)
    with pytest.raises(OSError, match="disk"):
        write_gate_record("o-r", 5, "b" * 40, tmp_path)
    assert read_gate_record("o-r", 5, tmp_path) == SHA  # old record intact
    assert [p.name for p in (tmp_path / "o-r").iterdir()] == ["build_gate_5.json"]


@pytest.mark.parametrize("sha", ["abc", "g" * 40, "a" * 39, "", "a" * 40 + "\n"])
def test_short_or_bad_sha_rejected_and_nothing_written(sha: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="40-character"):
        write_gate_record("o-r", 5, sha, tmp_path)
    assert not (tmp_path / "o-r").exists()


@pytest.mark.parametrize("slug", [".", "..", "a\n", "", "a/b"])
def test_validate_repo_slug_rejects_traversal_and_newline(slug: str) -> None:
    with pytest.raises(ValueError, match="Invalid repo slug"):
        validate_repo_slug(slug)


def test_read_missing_or_corrupt_is_none(tmp_path: Path) -> None:
    assert read_gate_record("o-r", 1, tmp_path) is None
    p = gate_record_path("o-r", 1, tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("not json")
    assert read_gate_record("o-r", 1, tmp_path) is None
    p.write_text('{"head_sha": ""}')
    assert read_gate_record("o-r", 1, tmp_path) is None


def test_read_ignores_extra_keys(tmp_path: Path) -> None:
    p = gate_record_path("o-r", 1, tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"head_sha": SHA, "other": 1}))
    assert read_gate_record("o-r", 1, tmp_path) == SHA


def test_no_harness_import() -> None:
    tree = ast.parse(Path(gaterecord.__file__).read_text())
    names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(n.split(".")[0] in {"harness", "spec_prism_flow"} for n in names)
