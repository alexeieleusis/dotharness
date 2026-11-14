"""The packaged refactor prompt: required text, prohibited text, substitution."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dotharness.sisyphus import prbody
from dotharness.sisyphus.orchestrator import build_orchestration_prompt
from dotharness.sisyphus.paths import knowledge_file

NAME = "sisyphus/refactor-prompt.md"
GROUP_FILES = ["src/a.py", "src/pkg/b.py"]
VALUES = {
    "stage": "refactor",
    "work-item reference": "#1",
    "brief path": "/w/brief.md",
    "handoff dir": "/w/handoffs",
    "target repo dir": "/w/clone",
    "target branch": "feature/x",
    "max retry cycles": "3",
    "stop report path": "/w/stop.json",
    "refactor_set": "fastapi",
    "group_files": "\n".join(GROUP_FILES),
    "input_file": "/w/refactor-input.json",
    "patterns dir": "/k/patterns",
}


def raw() -> str:
    return knowledge_file(NAME).read_text(encoding="utf-8")


def test_file_is_readable() -> None:
    assert knowledge_file(NAME).is_file()


@pytest.mark.parametrize(
    "needle",
    [
        "run_in_background",
        "foreground",
        "git status",
        "clean",
        "push",
        "one sub-agent per file",
        "C3",
        "C4",
        "C5",
        "checkpoint",
        "leaf",
        "detail",
        "<stop report path>",
        "QUICK_START_MIGRATION.md",
    ],
)
def test_required_text(needle: str) -> None:
    assert needle in raw()


@pytest.mark.parametrize(
    "needle",
    [
        "either `plan` or `build`",
        "<plan prompt path>",
        "<build prompt path>",
        "<phase config path>",
        "<cheat sheet path>",
        "<handoff_dir>",
        "NN-",
        "leaf handoffs",
    ],
)
def test_prohibited_text(needle: str) -> None:
    assert needle not in raw()


def test_substitution_leaves_no_angle_text() -> None:
    text = build_orchestration_prompt(raw(), VALUES)
    assert re.findall(r"<[^<>\n]+>", text) == []


def test_substituted_values_present() -> None:
    text = build_orchestration_prompt(raw(), VALUES)
    assert "fastapi" in text
    assert "/k/patterns/fastapi/QUICK_START_MIGRATION.md" in text
    for line in GROUP_FILES:
        assert line in text.splitlines()
    assert "/w/refactor-input.json" in text


def test_ends_with_newline_and_handoff_last() -> None:
    text = raw()
    assert text.endswith("\n")
    headings = re.findall(r"^## .*$", text, flags=re.M)
    assert headings[-2:] == ["## When to stop", "## Handoff"]


def _section(text: str) -> str:
    return text[text.index("## Handoff") :]


def _render_handoff(set_name: str) -> str:
    values = {**VALUES, "refactor_set": set_name}
    return _section(build_orchestration_prompt(raw(), values))


EDITS_BULLET = re.compile(r"^- Edits to existing files:[ \t]*(.*)$")


def _blocks(section: str) -> list[str]:
    return re.findall(r"^```\n(.*?)^```$", section, flags=re.M | re.S)


def test_raw_section_has_bullets_and_names() -> None:
    section = _section(raw())
    assert "\n- Deviations from the plan:" in section
    assert "\n- Edits to existing files:" in section
    assert "refactor-<refactor_set>.md" in section
    assert "<handoff dir>" in section
    assert "none" in section


def test_rendered_section_text() -> None:
    section = _render_handoff("fastapi")
    assert "refactor-fastapi.md" in section
    assert "/w/handoffs" in section
    for bad in ("NN-", "<stem>", "followup", "leaf handoff", "handoffs/"):
        assert bad not in section
    assert re.findall(r"<[^<>\n]+>", section) == []


def test_example_handoffs_parse(tmp_path: Path) -> None:
    blocks = _blocks(_render_handoff("fastapi"))
    assert len(blocks) == 2
    path = tmp_path / "refactor-fastapi.md"
    results = []
    for block in blocks:
        path.write_text(block, encoding="utf-8")
        first, second = block.splitlines()[:2]
        assert prbody._DEVIATION_BULLET.match(first)
        assert EDITS_BULLET.match(second)
        results.append(prbody._handoff_deviation("refactor-fastapi", path))
    assert results[0] == (None, None)
    entry, note = results[1]
    assert note is None
    assert entry is not None
    assert entry.startswith("- refactor-fastapi: skipped ")
    m = EDITS_BULLET.match(blocks[1].splitlines()[1])
    assert m is not None
    items = m.group(1).split("; ")
    assert len(items) == 2
    for item in items:
        assert re.fullmatch(r"\S+ \(.+\)", item)
    assert not items[0].startswith("/")


def test_handoff_name_does_not_collide_with_leaf(tmp_path: Path) -> None:
    (tmp_path / "refactor-fastapi.md").write_text("- x\n", encoding="utf-8")
    assert prbody.select_handoffs(tmp_path, ["01-example"]) == {"01-example": []}
