"""Tests for the local-review finding format, id stamping, change context and prompts."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path

import pytest

from harness.runners import local_review as lr
from harness.runners.local_review import (
    LocalReviewTarget,
    build_change_context,
    build_change_description,
    build_design_prompt,
    build_file_prompt,
    build_summary_prompt,
    finding_id,
    parse_findings,
    stamp_findings,
    stamp_findings_file,
)

KNOWLEDGE = Path(__file__).resolve().parents[4] / "knowledge" / "pr-review"

BLOCK_A = """## Finding: Off by one
- severity: P0
- file: src/a.py
- line: 12
- status: open

Loop skips the last item for `[1]`. Use `<=`."""

BLOCK_B = """## Finding: Missing check
- severity: P1
- file: src/b.py
- line: 1
- status: open

No validation of `None`."""


def sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def test_stamp_id_is_pass_plus_sha256_prefix_of_original_block() -> None:
    res = stamp_findings(BLOCK_A + "\n\n" + BLOCK_B + "\n", "file")
    assert res.stamped == 2
    assert [b.id for b in res.blocks] == [f"file-{sha8(BLOCK_A)}", f"file-{sha8(BLOCK_B)}"]
    assert finding_id("design", BLOCK_A) == f"design-{sha8(BLOCK_A)}"
    assert f"- status: open\n- id: file-{sha8(BLOCK_A)}\n\nLoop" in res.text


def test_restamp_after_text_edit_keeps_ids() -> None:
    first = stamp_findings(BLOCK_A + "\n", "design")
    edited = first.text.replace("Use `<=`.", "Totally rewritten body.").replace("status: open", "status: wontfix")
    second = stamp_findings(edited, "design")
    assert second.stamped == 0
    assert second.already_stamped == 1
    assert second.blocks[0].id == first.blocks[0].id
    assert second.blocks[0].status == "wontfix"
    assert second.text == edited


def test_restamp_is_idempotent() -> None:
    first = stamp_findings(BLOCK_A + "\n" + "\n" + BLOCK_B, "file")
    assert stamp_findings(first.text, "file").text == first.text


@pytest.mark.parametrize("missing", ["severity", "file", "line", "status"])
def test_missing_required_field_unstamped_counted_and_warned(missing: str, caplog: pytest.LogCaptureFixture) -> None:
    bad = "\n".join(ln for ln in BLOCK_A.splitlines() if not ln.startswith(f"- {missing}:"))
    text = bad + "\n\n" + BLOCK_B + "\n"
    with caplog.at_level(logging.WARNING, logger=lr.logger.name):
        res = stamp_findings(text, "file")
    assert res.unparseable == 1
    assert res.stamped == 1
    assert bad in res.text
    assert "- id:" not in res.blocks[0].text
    assert not res.blocks[0].valid
    assert "missing a required field" in caplog.text


@pytest.mark.parametrize(
    "old,new",
    [
        ("severity: P0", "severity: P2"),
        ("line: 12", "line: twelve"),
        ("status: open", "status: maybe"),
        ("file: src/a.py", "file: "),
    ],
)
def test_invalid_values_are_unparseable(old: str, new: str) -> None:
    res = stamp_findings(BLOCK_A.replace(old, new), "file")
    assert (res.stamped, res.unparseable) == (0, 1)


def test_summary_section_and_prose_are_not_findings_and_preserved() -> None:
    text = "intro\n\n" + BLOCK_A + "\n\n## Summary\nAll fine.\n"
    res = stamp_findings(text, "design")
    assert len(res.blocks) == 1
    assert res.text.endswith("\n\n## Summary\nAll fine.\n")
    assert res.text.startswith("intro\n\n")
    assert res.blocks[0].end_line <= res.text.splitlines().index("## Summary")


def test_heading_inside_code_fence_does_not_split_block() -> None:
    block = BLOCK_A + "\n\n```md\n## Finding: fake\n## Other\n```\nmore"
    res = stamp_findings(block, "file")
    assert len(res.blocks) == 1
    assert res.stamped == 1
    assert res.blocks[0].id == f"file-{sha8(block)}"  # hash over whole original block


def test_no_findings_and_empty_input() -> None:
    assert stamp_findings("", "file").blocks == []
    res = stamp_findings("No findings.\n", "file")
    assert (res.stamped, res.unparseable, res.text) == (0, 0, "No findings.\n")


def test_parse_fields() -> None:
    (blk,) = parse_findings(BLOCK_A)
    assert (blk.title, blk.severity, blk.file, blk.line, blk.status, blk.id, blk.valid) == (
        "Off by one",
        "P0",
        "src/a.py",
        12,
        "open",
        None,
        True,
    )


@pytest.mark.parametrize("status", ["open", "wontfix", "fixed", "declined"])
def test_all_statuses_valid(status: str) -> None:
    assert stamp_findings(BLOCK_A.replace("status: open", f"status: {status}"), "file").stamped == 1


def test_invalid_pass_name() -> None:
    with pytest.raises(ValueError, match="pass_name"):
        stamp_findings(BLOCK_A, "summary")


def test_stamp_file_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text(BLOCK_A + "\n", encoding="utf-8")
    assert stamp_findings_file(f, "file").stamped == 1
    mtime_text = f.read_text(encoding="utf-8")
    assert stamp_findings_file(f, "file").stamped == 0
    assert f.read_text(encoding="utf-8") == mtime_text


# ---- change context -------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, LocalReviewTarget]:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-qb", "feat/x")
    return tmp_path, LocalReviewTarget("feat/x", "main", base, base)


def _commit(path: Path, n: int, body: str = "") -> None:
    (path / "f.txt").write_text(f"{n}\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", f"commit {n}\n\n{body}" if body else f"commit {n}")


def test_description_small(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    _commit(path, 1, "body one")
    _commit(path, 2)
    desc = build_change_description(path, tgt)
    assert desc.startswith(f"## Change description\nBranch: feat/x\nBase ref: main\nMerge base: {tgt.base_sha}\n")
    assert desc.index("commit 1") < desc.index("commit 2")
    assert "body one" in desc
    assert "(truncated)" not in desc


def test_description_commit_cap(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    for i in range(1, 53):
        _commit(path, i)
    desc = build_change_description(path, tgt)
    assert desc.endswith("\n(truncated)")
    assert desc.count(" commit ") == 50
    assert len(desc) <= lr.MAX_DESCRIPTION_CHARS


def test_description_char_cap(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    for i in range(1, 6):
        _commit(path, i, "x" * 6000)
    desc = build_change_description(path, tgt)
    assert desc.endswith("\n(truncated)")
    assert len(desc) <= lr.MAX_DESCRIPTION_CHARS


def test_context_repo_name_and_fallback(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    named = build_change_context("my-repo", path, tgt, "## Change description\nx")
    assert "Repo: my-repo\n" in named
    assert f"Commit: {tgt.head_sha}" in named
    assert "PR URL" not in named
    assert "PR number" not in named
    assert f"Repo: {path.name}\n" in build_change_context("", path, tgt, "d")


# ---- prompts / templates --------------------------------------------------


@pytest.mark.parametrize("name", list(lr.REVIEW_TEMPLATES))
def test_templates_are_local(name: str) -> None:
    text = (KNOWLEDGE / name).read_text(encoding="utf-8")
    assert lr.OUTPUT_FILE_PLACEHOLDER in text
    assert "dotharness-review-" not in text
    assert "gh api" not in text
    assert "gh pr comment" not in text


@pytest.mark.parametrize("name", [lr.FILE_TEMPLATE, lr.DESIGN_TEMPLATE])
def test_finding_templates_spec_format(name: str) -> None:
    text = (KNOWLEDGE / name).read_text(encoding="utf-8")
    assert "## Finding: <short title>\n- severity: P0 | P1\n- file: " in text
    assert "- status: open" in text


def test_prompts_assemble(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    file_t = (KNOWLEDGE / lr.FILE_TEMPLATE).read_text(encoding="utf-8")
    p = build_file_prompt(file_t, out, "\n\n## Diff for a\nD", "\n\nRepo: r", "GUIDE", "SONAR")
    assert str(out.resolve()) in p
    assert lr.OUTPUT_FILE_PLACEHOLDER not in p
    assert "## Additional Review Guide\nGUIDE" in p
    assert "## Static Analysis\nSONAR" in p
    s = build_summary_prompt("S {OUTPUT_FILE}", out, ["a.py", "b.py"], "\n\nRepo: r")
    assert s.endswith("Files reviewed:\na.py\nb.py")
    assert "## Additional Review Guide" not in s
    d = build_design_prompt("D {OUTPUT_FILE}", out, "\n\nDIFFS", "\n\nRepo: r")
    assert str(out.resolve()) in d
    assert "DIFFS" in d
