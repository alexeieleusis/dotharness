from __future__ import annotations

import ast
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sisyphus import digest
from sisyphus.config import DigestConfig, ProjectConfig, SisyphusConfig
from sisyphus.digest import (
    SECTIONS,
    DeclinedEntry,
    DigestReport,
    DigestWriteError,
    DoneEntry,
    NeedsYou,
    StageTiming,
    render_digest,
    write_digest,
)
from sisyphus.runrecord import RunRecord


def _config(path: str | None, workspace: str | None = None) -> SisyphusConfig:
    return SisyphusConfig(project=ProjectConfig(workspace_dir=workspace), digest=DigestConfig(path_template=path))


def _headings(text: str) -> list[str]:
    return [line[3:] for line in text.splitlines() if line.startswith("## ")]


def test_success_run_has_six_sections_in_order(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path / "ws", "ENG-1")
    rec.mark_stage("build", "complete")
    report = DigestReport(done=(DoneEntry("PR #5 opened", ("make check ok", "make test ok")),))
    text = render_digest([rec], report)
    assert _headings(text) == list(SECTIONS)
    assert "PR #5 opened (evidence: make check ok; make test ok)" in text
    assert "## Needs you\n\nNone" in text
    assert "## Declined\n\nNone" in text


def test_early_failure_run_has_six_sections(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path / "ws", "ENG-2")
    rec.mark_stage("intake")
    rec.set_checkpoint("C1")
    text = render_digest([rec])
    assert _headings(text) == list(SECTIONS)
    assert "1. [C1] ENG-2:" in text
    assert "Stage reached: intake (in_progress)" in text


def test_empty_run_still_renders() -> None:
    text = render_digest([])
    assert _headings(text) == list(SECTIONS)
    assert text.count("\nNone\n") == 6


def test_ci_fix_and_merge_outcomes_from_records(tmp_path: Path) -> None:
    ci = RunRecord.open(tmp_path / "ws", "PR-7")
    ci.mark_stage("ci-fix", "complete")
    ci.increment_retry("ci-fix:7")
    merge = RunRecord.open(tmp_path / "ws", "PR-8")
    merge.mark_stage("merge", "in_progress")
    text = render_digest([ci, merge])
    assert "PR-7: ci-fix complete (retries: ci-fix:7=1)" in text
    assert "PR-8: merge in_progress and did not finish" in text
    done = text.split("## Done")[1].split("## Declined")[0]
    declined = text.split("## Declined")[1].split("## Deviations")[0]
    assert "PR-7" in done
    assert "PR-8" in declined


def test_report_entries_and_limits(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path / "ws", "ENG-3")
    rec.set_checkpoint("C6")
    report = DigestReport(
        needs_you=(NeedsYou("ENG-3", "C6", "Raise max_run_minutes?"),),
        declined=(DeclinedEntry("CI job lint on PR 9", "did not reproduce locally"),),
        deviations=("leaf 3 renamed a module",),
        limits_hit=("max_leaves_per_run reached",),
        timings=(StageTiming("ENG-3", "build", 12.4, 1000), StageTiming("ENG-3", "plan", 3.0)),
    )
    text = render_digest([rec], report)
    assert "1. [C6] ENG-3: Raise max_run_minutes?" in text
    assert text.count("[C6] ENG-3") == 1  # not duplicated by the record
    assert "did not reproduce locally" in text
    assert "leaf 3 renamed a module" in text
    assert "max_leaves_per_run reached" in text
    assert "ENG-3: checkpoint C6 raised" in text
    assert "ENG-3 / build: 12s, 1000 tokens" in text
    assert "ENG-3 / plan: 3s, tokens not reported" in text


def test_write_digest_expands_template(tmp_path: Path) -> None:
    cfg = _config("<workspace_dir>/out/digest-<date>.md", str(tmp_path))
    path = write_digest([], cfg, today=datetime(2026, 10, 16, tzinfo=UTC))
    assert path == tmp_path / "out" / "digest-2026-10-16.md"
    assert _headings(path.read_text()) == list(SECTIONS)
    assert len(list((tmp_path / "out").iterdir())) == 1


def test_second_write_same_day_keeps_first_digest(tmp_path: Path) -> None:
    cfg = _config(str(tmp_path / "digest-<date>.md"))
    today = datetime(2026, 10, 16, tzinfo=UTC)
    first = write_digest([], cfg, DigestReport(deviations=("first run",)), today=today)
    first_text = first.read_text()
    second = write_digest([], cfg, DigestReport(deviations=("second run",)), today=today)
    third = write_digest([], cfg, today=today)
    assert first == tmp_path / "digest-2026-10-16.md"
    assert second == tmp_path / "digest-2026-10-16-2.md"
    assert third == tmp_path / "digest-2026-10-16-3.md"
    assert first.read_text() == first_text
    assert "first run" in first_text
    assert "second run" in second.read_text()
    assert sorted(p.name for p in tmp_path.iterdir()) == [first.name, second.name, third.name]


def test_missing_template_raises() -> None:
    with pytest.raises(DigestWriteError):
        write_digest([], _config(None))
    with pytest.raises(DigestWriteError, match="workspace_dir"):
        write_digest([], _config("<workspace_dir>/d.md"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_path_raises(tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        with pytest.raises(DigestWriteError, match="cannot write digest"):
            write_digest([], _config(str(ro / "sub" / "d.md")))
    finally:
        ro.chmod(0o755)


def test_no_harness_imports() -> None:
    tree = ast.parse(Path(digest.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in mods if m.split(".")[0] in ("harness", "spec_prism_flow")}


def test_retry_note_matches_exact_stage_or_stage_colon_keys_only(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path / "ws", "PR-9")
    rec.mark_stage("merge", "in_progress")
    for key in ("merge", "merge:attempts", "merged-elsewhere", "merge-queue:1", "build:retry-cycles", "auto-retry"):
        rec.increment_retry(key)
    text = render_digest([rec])
    assert "PR-9: merge in_progress (retries: merge=1 merge:attempts=1) and did not finish" in text
