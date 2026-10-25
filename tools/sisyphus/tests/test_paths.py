from __future__ import annotations

import re
from pathlib import Path

import pytest

from sisyphus.paths import (
    PathResolutionError,
    find_checkout_root,
    knowledge_dir,
    knowledge_file,
    tool_dir,
)


def _installed(home: Path) -> None:
    (home / ".harness" / "tools" / "spec-prism-flow").mkdir(parents=True)
    (home / ".harness" / "knowledge" / "sisyphus").mkdir(parents=True)
    (home / ".harness" / "knowledge" / "sisyphus" / "a.md").write_text("x")


def _checkout(root: Path) -> None:
    (root / "tools" / "spec-prism-flow").mkdir(parents=True)
    (root / "knowledge" / "sisyphus").mkdir(parents=True)
    (root / "knowledge" / "sisyphus" / "a.md").write_text("x")


def test_installed_first(tmp_path: Path) -> None:
    home, root = tmp_path / "h", tmp_path / "r"
    _installed(home)
    _checkout(root)
    assert tool_dir(home, root) == (home / ".harness/tools/spec-prism-flow").resolve()
    assert knowledge_dir(home, root) == (home / ".harness/knowledge").resolve()
    assert knowledge_file("sisyphus/a.md", home, root) == (home / ".harness/knowledge/sisyphus/a.md").resolve()
    assert tool_dir(home, root).is_absolute()


def test_checkout_fallback(tmp_path: Path) -> None:
    home, root = tmp_path / "h", tmp_path / "r"
    home.mkdir()
    _checkout(root)
    assert tool_dir(home, root) == (root / "tools/spec-prism-flow").resolve()
    assert knowledge_dir(home, root) == (root / "knowledge").resolve()
    assert knowledge_file("knowledge/sisyphus/a.md", home, root) == (root / "knowledge/sisyphus/a.md").resolve()


def test_neither_names_all_tried(tmp_path: Path) -> None:
    home, root = tmp_path / "h", tmp_path / "r"
    home.mkdir()
    root.mkdir()
    with pytest.raises(PathResolutionError) as exc:
        tool_dir(home, root)
    assert str(home / ".harness/tools/spec-prism-flow") in str(exc.value)
    assert str(root / "tools/spec-prism-flow") in str(exc.value)
    with pytest.raises(PathResolutionError):
        knowledge_file("sisyphus/missing.md", home, root)


def test_knowledge_file_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(PathResolutionError):
        knowledge_file("../x", tmp_path, tmp_path)
    with pytest.raises(PathResolutionError):
        knowledge_file("/etc/passwd", tmp_path, tmp_path)


def test_find_checkout_root(tmp_path: Path) -> None:
    _checkout(tmp_path)
    deep = tmp_path / "tools" / "sisyphus" / "sisyphus" / "x.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("")
    assert find_checkout_root(deep) == tmp_path.resolve()


def test_real_checkout_resolves(tmp_path: Path) -> None:
    root = find_checkout_root()
    assert root is not None
    assert knowledge_dir(tmp_path, root) == (root / "knowledge").resolve()


def test_linear_feed_prompt_resolves_with_one_placeholder(tmp_path: Path) -> None:
    root = find_checkout_root()
    assert root is not None
    path = knowledge_file("sisyphus/linear-feed-prompt.md", tmp_path, root)
    assert path == (root / "knowledge/sisyphus/linear-feed-prompt.md").resolve()
    text = path.read_text()
    assert text.count("<config path>") == 1
    assert re.findall(r"<[^>\n]+>", text) == ["<config path>"]
    for name in ("dir", "assignee", "pick_state", "pick_label"):
        assert f"`{name}`" in text
    assert "[source.work_item]" in text
