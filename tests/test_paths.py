import ast
import re
from importlib import resources
from pathlib import Path

import pytest

import dotharness.paths as paths
from dotharness.paths import (
    build_gate_record_path,
    default_knowledge_dir,
    fallback_repo_slug,
    repo_slug_from_name,
    validate_repo_slug,
)
from dotharness.pr_review import config as pr_config


def test_packaged_default_when_home_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_knowledge_dir() == Path(str(resources.files("dotharness") / "knowledge"))


def test_home_dir_wins_when_present(tmp_path, monkeypatch):
    (tmp_path / ".harness" / "knowledge").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_knowledge_dir() == tmp_path / ".harness" / "knowledge"


def test_pr_review_config_reexports_shared_function():
    assert pr_config.default_knowledge_dir is default_knowledge_dir


def test_harness_section_default_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pr_config.HarnessSection().knowledge_dir == default_knowledge_dir()
    explicit = tmp_path / "custom"
    assert pr_config.HarnessSection(knowledge_dir=explicit).knowledge_dir == explicit


@pytest.mark.parametrize("slug", ["x", "Oscilar-backend", "a.b_c-1", ".hidden", "a..b"])
def test_validate_repo_slug_accepts(slug):
    assert validate_repo_slug(slug) == slug


@pytest.mark.parametrize("slug", [".", "..", "", "a/b", "a b", "a\n"])
def test_validate_repo_slug_rejects(slug):
    with pytest.raises(ValueError, match="Invalid repo slug"):
        validate_repo_slug(slug)


def test_build_gate_record_path_uses_home_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = build_gate_record_path("x", 12)
    assert path == tmp_path / ".local/share/dotharness/state/x/build_gate_12.json"
    assert not (tmp_path / ".local").exists()  # pure: nothing created


def test_build_gate_record_path_follows_patched_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    assert build_gate_record_path("x", 3) == tmp_path / "state/x/build_gate_3.json"


def test_build_gate_record_path_rejects_bad_slug_before_building(monkeypatch):
    def boom():
        raise AssertionError("data_dir must not be called")  # noqa: TRY003

    monkeypatch.setattr(paths, "data_dir", boom)
    with pytest.raises(ValueError, match="Invalid repo slug"):
        build_gate_record_path("../etc", 1)


def test_fallback_repo_slug_shape_and_golden(tmp_path):
    d = tmp_path / "My Repo.git"
    d.mkdir()
    slug = fallback_repo_slug(d)
    assert re.fullmatch(r"My-Repo\.git-[0-9a-f]{8}", slug)
    assert fallback_repo_slug(d) == slug
    assert validate_repo_slug(slug) == slug


def test_paths_module_has_no_pr_review_import():
    tree = ast.parse(Path(paths.__file__).read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not [n for n in names if n.startswith("dotharness.pr_review")]


def test_repo_slug_from_name_replaces_slash_then_validates():
    assert repo_slug_from_name("owner/repo") == "owner-repo"
    for bad in ("owner/re po", ".."):
        with pytest.raises(ValueError, match="Invalid repo slug"):
            repo_slug_from_name(bad)
