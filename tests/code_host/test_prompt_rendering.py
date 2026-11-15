"""Prompt templates name no host; the reference host's variables render today's text byte for byte."""

import re
from pathlib import Path

import pytest

from dotharness.code_host.reference import PROMPT_VARIABLES, PromptVariableError, render_prompt
from dotharness.pr_review.runners.local_review.prompts import build_file_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "src" / "dotharness" / "knowledge" / "pr-review"
BASELINE = REPO_ROOT / "tests" / "data" / "prompt_baseline"  # The files as they were before the variables.
ACTION_PROMPTS = (
    "review-summary.md",
    "review-file.md",
    "review-design.md",
    "review-traceability.md",
    "focused-review.md",
    "address-comment.md",
)
PROHIBITION_PROMPTS = (
    "local-review-file.md",
    "local-review-design.md",
    "local-review-summary.md",
    "local-address-finding.md",
    "review-regret.md",
)
ALL_PROMPTS = ACTION_PROMPTS + PROHIBITION_PROMPTS


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def test_baseline_covers_every_changed_prompt() -> None:
    assert {p.name for p in BASELINE.glob("*.md")} == set(ALL_PROMPTS)


@pytest.mark.parametrize("name", ALL_PROMPTS)
def test_reference_rendering_equals_the_frozen_baseline(name: str) -> None:
    assert render_prompt(_read(TEMPLATES / name)) == _read(BASELINE / name)


@pytest.mark.parametrize("name", ALL_PROMPTS)
def test_template_names_no_gh_and_declares_what_it_uses(name: str) -> None:
    text = _read(TEMPLATES / name)
    assert not re.search(r"\bgh\b", text)
    declared = set(text.split("\n", 1)[0].removeprefix("<!-- host-variables: ").removesuffix(" -->").split(", "))
    assert declared <= PROMPT_VARIABLES.keys()
    assert re.search(r"\bgh\b", _read(BASELINE / name))


@pytest.mark.parametrize("name", PROHIBITION_PROMPTS)
def test_prohibition_prompts_use_only_the_cli_name(name: str) -> None:
    assert _read(TEMPLATES / name).startswith("<!-- host-variables: host-cli -->\n\n")


def test_render_is_idempotent_and_leaves_plain_text() -> None:
    rendered = render_prompt(_read(TEMPLATES / "review-summary.md"))
    assert render_prompt(rendered) == rendered
    assert render_prompt("no preamble <host-cli>") == "no preamble <host-cli>"


def test_render_uses_the_variables_the_host_supplies() -> None:
    text = "<!-- host-variables: host-cli -->\n\nRun <host-cli> now."
    assert render_prompt(text, {"host-cli": "tool"}) == "Run tool now."


@pytest.mark.parametrize(
    ("text", "values"),
    [
        ("<!-- host-variables: host-cli -->\n\nNothing used.", {"host-cli": "x"}),
        ("<!-- host-variables: host-cli -->\n\n<host-cli> <host-other>", {"host-cli": "x", "host-other": "y"}),
        ("<!-- host-variables: host-cli -->\n\n<host-cli>", {}),
    ],
)
def test_render_rejects_mismatched_or_unsupplied_variables(text: str, values: dict[str, str]) -> None:
    with pytest.raises(PromptVariableError):
        render_prompt(text, values)


def test_local_review_builders_render_the_template() -> None:
    template = _read(TEMPLATES / "local-review-file.md")
    baseline = _read(BASELINE / "local-review-file.md")
    out = (REPO_ROOT / "out.md").resolve()
    expected = baseline.replace("{OUTPUT_FILE}", str(out)) + "\n\nSECTION\n\nCTX"
    assert build_file_prompt(template, out, "\n\nSECTION", "\n\nCTX") == expected
