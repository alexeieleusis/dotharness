"""The packaged refactor prompt: required text, prohibited text, substitution."""

from __future__ import annotations

import re

import pytest

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


def test_ends_with_newline_and_when_to_stop() -> None:
    text = raw()
    assert text.endswith("\n")
    assert re.findall(r"^## .*$", text, flags=re.M)[-1] == "## When to stop"
