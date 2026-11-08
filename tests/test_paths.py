from importlib import resources
from pathlib import Path

from dotharness.paths import default_knowledge_dir
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
