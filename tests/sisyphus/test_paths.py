from __future__ import annotations

import re
from pathlib import Path

import pytest

from dotharness.sisyphus.paths import PathResolutionError, knowledge_file

PACKAGED = Path(__file__).parents[2] / "src" / "dotharness" / "knowledge"


def test_packaged_file_resolves_when_no_user_dir() -> None:
    # conftest points HOME at an empty dir, so there is no ~/.harness/knowledge
    path = knowledge_file("sisyphus/intake-prompt.md")
    assert path == (PACKAGED / "sisyphus/intake-prompt.md").resolve()
    assert knowledge_file("knowledge/sisyphus/intake-prompt.md") == path


def test_cheat_sheet_is_packaged() -> None:
    assert knowledge_file("spec-prism-flow/lifecycle-cheat-sheet.md").is_file()


def test_user_dir_wins_then_falls_back_to_packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    user = tmp_path / ".harness" / "knowledge" / "sisyphus"
    user.mkdir(parents=True)
    (user / "intake-prompt.md").write_text("mine")
    assert knowledge_file("sisyphus/intake-prompt.md") == (user / "intake-prompt.md").resolve()
    # a user dir that lacks a file must not hide the packaged copy
    assert knowledge_file("sisyphus/sizing-rules.md") == (PACKAGED / "sisyphus/sizing-rules.md").resolve()


def test_missing_names_every_path_tried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".harness" / "knowledge").mkdir(parents=True)
    with pytest.raises(PathResolutionError) as exc:
        knowledge_file("sisyphus/missing.md")
    assert str(tmp_path / ".harness/knowledge/sisyphus/missing.md") in str(exc.value)
    assert str(PACKAGED / "sisyphus/missing.md") in str(exc.value)


def test_knowledge_file_rejects_escape() -> None:
    with pytest.raises(PathResolutionError):
        knowledge_file("../x")
    with pytest.raises(PathResolutionError):
        knowledge_file("/etc/passwd")


def test_linear_feed_prompt_resolves_with_one_placeholder() -> None:
    path = knowledge_file("sisyphus/linear-feed-prompt.md")
    text = path.read_text()
    assert text.count("<config path>") == 1
    assert re.findall(r"<[^>\n]+>", text) == ["<config path>"]
    for name in ("dir", "assignee", "pick_state", "pick_label"):
        assert f"`{name}`" in text
    assert "[source.work_item]" in text


@pytest.mark.parametrize("name", ["plan-generation-prompt.md", "build-execution-prompt.md", "lifecycle-cheat-sheet.md"])
def test_packaged_spec_prism_flow_files_use_migrated_names(name: str) -> None:
    text = knowledge_file(f"spec-prism-flow/{name}").read_text()
    assert "spec-prism-flow plan" not in text
    assert "spec-prism-flow build" not in text
    assert "~/.harness/tools" not in text
    assert "<tool repo>" not in text
