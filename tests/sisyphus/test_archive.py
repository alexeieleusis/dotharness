from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from dotharness.sisyphus.archive import (
    archive_item,
    check_archive_dir,
    item_files,
    resolve_archive_dir,
)
from dotharness.sisyphus.config import ConfigError, SisyphusConfig, load_config
from dotharness.sisyphus.orchestrator import _Run
from dotharness.sisyphus.runrecord import GATE_FAILED_KEY, RunRecord
from dotharness.sisyphus.settled import LEGACY_FINISHED_STAGE, is_settled
from dotharness.sisyphus.stages import OPEN_PR_STAGE
from dotharness.sisyphus.stoprecord import build_abandoned_record, build_stop_record, write_stop_record
from tests.sisyphus.test_cli import make_env


def test_absolute_dir(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    assert resolve_archive_dir(str(other), str(tmp_path / "ws")) == other
    assert resolve_archive_dir(str(other), None) == other


def test_relative_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert resolve_archive_dir("archive", str(ws)) == ws / "archive"


def test_relative_dir_without_workspace() -> None:
    with pytest.raises(ConfigError, match="workspace_dir"):
        resolve_archive_dir("archive", None)


def test_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_archive_dir("~/arch", "~/ws") == tmp_path / "arch"
    assert resolve_archive_dir("arch", "~/ws") == tmp_path / "ws" / "arch"


def test_resolver_creates_nothing(tmp_path: Path) -> None:
    resolve_archive_dir("archive", str(tmp_path / "ws"))
    assert list(tmp_path.iterdir()) == []


def test_check_ok(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    check_archive_dir(ws / "archive", str(ws))
    check_archive_dir(tmp_path / "other", str(ws))
    assert list(tmp_path.iterdir()) == []


def test_check_equal(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ConfigError):
        check_archive_dir(ws, str(ws))
    with pytest.raises(ConfigError):
        check_archive_dir(ws / "sub" / "..", str(ws))


def test_check_contains(tmp_path: Path) -> None:
    ws = tmp_path / "a" / "ws"
    ws.mkdir(parents=True)
    with pytest.raises(ConfigError):
        check_archive_dir(tmp_path, str(ws))
    with pytest.raises(ConfigError):
        check_archive_dir(tmp_path / "a", str(ws))


def test_check_symlink_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    link = tmp_path / "link"
    link.symlink_to(ws)
    with pytest.raises(ConfigError):
        check_archive_dir(link, str(ws))


def test_check_unset_workspace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="workspace_dir"):
        check_archive_dir(tmp_path, None)


ITEM = "#147"
ENC = "_23147"


def _config(tmp_path: Path, brief_template: str | None = "<workspace_dir>/<item-id>.brief.md") -> SisyphusConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    lines = ["[project]", f'repo_dir = "{tmp_path / "repo"}"', f'workspace_dir = "{ws}"']
    if brief_template:
        lines.append(f'brief_path_template = "{brief_template}"')
    cfg_file = tmp_path / ".sisyphus.toml"
    cfg_file.write_text("\n".join(lines) + "\n")
    return load_config(cfg_file)


def _populate(cfg: SisyphusConfig, brief_dir: Path | None = None) -> RunRecord:
    ws = Path(cfg.project.workspace_dir or "")
    rec = RunRecord.open(ws, ITEM)
    (ws / f"{ENC}.json.stopped.json").write_text("stop")
    (ws / f"{ENC}.answer.md").write_text("answer")
    (ws / f"{ENC}.answer.md.used").write_text("used")
    (ws / "plan-147" / "handoffs").mkdir(parents=True)
    (ws / "plan-147" / "a.md").write_text("a")
    (ws / "plan-147" / "handoffs" / "h.md").write_text("h")
    brief = (brief_dir or ws) / f"{ENC}.brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("brief")
    (brief.parent / f"{ENC}.brief.md.intake.json").write_text("{}")
    # never part of the item
    (ws / "digest-1.md").write_text("d")
    (ws / "handoffs").mkdir()
    (ws / "handoffs" / "x.md").write_text("x")
    (ws / f"{ENC}.json.stop.json").write_text("legacy")
    return rec


def test_item_files_set_and_order(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    ws = tmp_path / "ws"
    files = item_files(cfg, rec)
    assert files[-1] == ws / f"{ENC}.json"
    assert set(files) == {
        ws / f"{ENC}.json",
        ws / f"{ENC}.json.stopped.json",
        ws / f"{ENC}.answer.md",
        ws / f"{ENC}.answer.md.used",
        ws / f"{ENC}.brief.md",
        ws / f"{ENC}.brief.md.intake.json",
        ws / "plan-147",
    }


def test_missing_files_skipped_silently(tmp_path: Path) -> None:
    cfg = _config(tmp_path, brief_template=None)
    rec = RunRecord.open(tmp_path / "ws", ITEM)
    assert item_files(cfg, rec) == [rec.path]
    res = archive_item(cfg, rec, tmp_path / "arch")
    assert res.moved == [tmp_path / "arch" / f"{ENC}.json"]
    assert not res.refused


def test_archive_moves_everything_flat(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    arch = tmp_path / "arch"
    res = archive_item(cfg, rec, arch)
    assert not res.refused
    assert res.warnings == []
    assert res.moved[-1] == arch / f"{ENC}.json"
    assert sorted(p.name for p in arch.iterdir()) == sorted([
        f"{ENC}.json",
        f"{ENC}.json.stopped.json",
        f"{ENC}.answer.md",
        f"{ENC}.answer.md.used",
        f"{ENC}.brief.md",
        f"{ENC}.brief.md.intake.json",
        "plan-147",
    ])
    assert (arch / "plan-147" / "handoffs" / "h.md").read_text() == "h"
    ws = tmp_path / "ws"
    assert sorted(p.name for p in ws.iterdir()) == sorted(["digest-1.md", f"{ENC}.json.stop.json", "handoffs"])


def test_brief_outside_workspace_moves_to_basename(tmp_path: Path) -> None:
    outside = tmp_path / "briefs"
    cfg = _config(tmp_path, brief_template=f"{outside}/<item-id>.brief.md")
    rec = _populate(cfg, brief_dir=outside)
    arch = tmp_path / "arch"
    res = archive_item(cfg, rec, arch)
    assert res.warnings == []
    assert (arch / f"{ENC}.brief.md").read_text() == "brief"
    assert (arch / f"{ENC}.brief.md.intake.json").exists()
    assert not outside.exists() or not list(outside.iterdir())


def test_missing_brief_warns_and_still_archives(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    rec = RunRecord.open(tmp_path / "ws", ITEM)
    res = archive_item(cfg, rec, tmp_path / "arch")
    assert not res.refused
    assert len(res.warnings) == 2
    assert "brief not found" in res.warnings[0]
    assert (tmp_path / "arch" / f"{ENC}.json").exists()
    assert not rec.path.exists()


def test_unset_brief_template_is_a_warning(tmp_path: Path) -> None:
    cfg = _config(tmp_path, brief_template=None)
    rec = RunRecord.open(tmp_path / "ws", ITEM)
    res = archive_item(cfg, rec, tmp_path / "arch")
    assert not res.refused
    assert len(res.warnings) == 1
    assert "brief_path_template" in res.warnings[0]


def test_collision_refuses_and_moves_nothing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    arch = tmp_path / "arch"
    (arch / "plan-147").mkdir(parents=True)  # a directory colliding with the plan tree
    res = archive_item(cfg, rec, arch)
    assert res.refused
    assert res.collision == arch / "plan-147"
    assert res.moved == []
    assert rec.path.exists()
    assert (tmp_path / "ws" / "plan-147" / "a.md").exists()
    assert (tmp_path / "ws" / f"{ENC}.answer.md").exists()
    assert list((arch / "plan-147").iterdir()) == []


def test_collision_on_run_record_never_overwrites(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    arch = tmp_path / "arch"
    arch.mkdir()
    (arch / f"{ENC}.json").write_text("other")
    res = archive_item(cfg, rec, arch)
    assert res.collision == arch / f"{ENC}.json"
    assert (arch / f"{ENC}.json").read_text() == "other"
    assert (tmp_path / "ws" / f"{ENC}.answer.md").exists()


def test_interrupted_run_rerun_completes(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    arch = tmp_path / "arch"
    ws = tmp_path / "ws"
    real_replace = os.replace
    calls = 0

    def crash(src: str | Path, dst: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError(errno.EACCES, "boom")
        real_replace(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", crash)
        with pytest.raises(PermissionError):
            archive_item(cfg, rec, arch)
    assert rec.path.exists()  # run record is last: item still in workspace
    assert (arch / f"{ENC}.json").exists() is False
    assert len(list(arch.iterdir())) == 2
    # files moved by the interrupted run are not collisions on rerun
    res = archive_item(cfg, rec, arch)
    assert not res.refused
    assert res.warnings == []
    assert not rec.path.exists()
    assert (arch / f"{ENC}.json").exists()
    assert (arch / "plan-147" / "a.md").exists()
    assert not (ws / "plan-147").exists()


def test_rerun_after_full_archive_is_noop(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    arch = tmp_path / "arch"
    archive_item(cfg, rec, arch)
    res = archive_item(cfg, rec, arch)
    assert not res.refused
    assert res.moved == []
    assert res.warnings == []


def test_cross_filesystem_copy_then_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    arch = tmp_path / "arch"

    def exdev(src: str | Path, dst: str | Path) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "replace", exdev)
    res = archive_item(cfg, rec, arch)
    assert not res.refused
    assert res.warnings == []
    assert res.moved[-1] == arch / f"{ENC}.json"
    assert (arch / "plan-147" / "handoffs" / "h.md").read_text() == "h"
    assert (arch / f"{ENC}.answer.md").read_text() == "answer"
    ws = tmp_path / "ws"
    assert not (ws / "plan-147").exists()
    assert not rec.path.exists()


def test_cross_filesystem_interrupted_tree_copy_leaves_no_partial_and_rerun_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    rec = _populate(cfg)
    arch = tmp_path / "arch"
    ws = tmp_path / "ws"

    def exdev(src: str | Path, dst: str | Path) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    real_copy2 = shutil.copy2
    real_copytree = shutil.copytree
    plan_copies = 0

    def flaky_copy2(src: Any, dst: Any, **kw: Any) -> Any:
        nonlocal plan_copies
        if "plan-147" in os.fspath(src):  # copytree passes os.DirEntry objects
            plan_copies += 1
            if plan_copies == 2:
                raise OSError(errno.ENOSPC, "no space")
        return real_copy2(src, dst, **kw)

    def patched_copytree(src: Any, dst: Any, *args: Any, **kw: Any) -> Any:
        # recursive calls pass copy_function positionally; only inject it on the outermost call
        if len(args) < 3:
            kw.setdefault("copy_function", flaky_copy2)
        return real_copytree(src, dst, *args, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(os, "replace", exdev)
        mp.setattr(shutil, "copy2", flaky_copy2)
        # copytree binds copy2 as a default argument, so patching the module attribute alone is not enough
        mp.setattr(shutil, "copytree", patched_copytree)
        with pytest.raises(OSError, match="no space"):
            archive_item(cfg, rec, arch)
    # the destination name never appears for an incomplete copy, and no partial is left behind
    assert not (arch / "plan-147").exists()
    assert not any(p.name.endswith(".partial") for p in arch.iterdir())
    assert (ws / "plan-147" / "handoffs" / "h.md").exists()
    assert rec.path.exists()

    monkeypatch.setattr(os, "replace", exdev)
    res = archive_item(cfg, rec, arch)
    assert not res.refused
    assert (arch / "plan-147" / "handoffs" / "h.md").read_text() == "h"
    assert (arch / "plan-147" / "a.md").read_text() == "a"
    assert not (ws / "plan-147").exists()
    assert not rec.path.exists()


def test_cross_filesystem_delete_failure_warns_and_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path, brief_template=None)
    rec = RunRecord.open(tmp_path / "ws", ITEM)
    answer = tmp_path / "ws" / f"{ENC}.answer.md"
    answer.write_text("answer")

    def exdev(src: str | Path, dst: str | Path) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    def no_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "replace", exdev)
    monkeypatch.setattr(Path, "unlink", no_unlink)
    res = archive_item(cfg, rec, tmp_path / "arch")
    assert not res.refused
    assert len(res.warnings) == 3  # brief template unset, answer, run record
    assert answer.exists()
    assert (tmp_path / "arch" / f"{ENC}.answer.md").read_text() == "answer"


def test_cross_filesystem_delete_failure_rerun_cleans_stale_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path, brief_template=None)
    rec = RunRecord.open(tmp_path / "ws", ITEM)
    answer = tmp_path / "ws" / f"{ENC}.answer.md"
    answer.write_text("answer")
    arch = tmp_path / "arch"

    def exdev(src: str | Path, dst: str | Path) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    def no_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(errno.EACCES, "denied")

    with monkeypatch.context() as mp:
        mp.setattr(os, "replace", exdev)
        mp.setattr(Path, "unlink", no_unlink)
        archive_item(cfg, rec, arch)
    assert answer.exists()
    assert rec.path.exists()
    res = archive_item(cfg, rec, arch)
    assert not res.refused
    assert not answer.exists()
    assert not rec.path.exists()
    assert (arch / f"{ENC}.answer.md").read_text() == "answer"


def test_other_oserror_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path, brief_template=None)
    rec = RunRecord.open(tmp_path / "ws", ITEM)

    def fail(src: str | Path, dst: str | Path) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="io"):
        archive_item(cfg, rec, tmp_path / "arch")
    assert rec.path.exists()


def _rec(tmp_path: Path, stage: str, status: str = "complete") -> RunRecord:
    rec = RunRecord.open(tmp_path / "ws", "X-1")
    rec.mark_stage(stage, status)
    return rec


@pytest.mark.parametrize("stage", [OPEN_PR_STAGE, LEGACY_FINISHED_STAGE])
def test_is_settled_finished(tmp_path: Path, stage: str) -> None:
    assert is_settled(_rec(tmp_path, stage), None)


def test_is_settled_unfinished_stage_or_status(tmp_path: Path) -> None:
    assert not is_settled(_rec(tmp_path, "build", "in_progress"), None)
    assert not is_settled(_rec(tmp_path, OPEN_PR_STAGE, "in_progress"), None)


def test_is_settled_checkpoint_and_stop_but_not_gate(tmp_path: Path) -> None:
    cp = _rec(tmp_path, OPEN_PR_STAGE)
    cp.set_checkpoint("C1")
    assert not is_settled(cp, None)
    gf = _rec(tmp_path, OPEN_PR_STAGE)
    gf.set_checkpoint(None)
    gf.retries[GATE_FAILED_KEY] = 1
    assert is_settled(gf, None)  # the gate-failed flag does not affect settledness
    gf.set_checkpoint("C1")
    assert not is_settled(gf, None)
    stop = build_stop_record(
        "X-1", checkpoint=None, stage=OPEN_PR_STAGE, stage_status="complete", detail="x", resume_command="r"
    )
    assert not is_settled(_rec(tmp_path, OPEN_PR_STAGE), stop)


def test_is_settled_abandoned_ignores_gate_failed(tmp_path: Path) -> None:
    rec = _rec(tmp_path, "build", "in_progress")
    rec.retries[GATE_FAILED_KEY] = 1
    stop = build_abandoned_record(
        "X-1", stage="build", stage_status="in_progress", prior_label="l", reenable_command="r"
    )
    assert is_settled(rec, stop)


def _rule(checkpoint: bool, stop: str, stage: str, status: str, gate_failed: int) -> bool:
    """The current settled rule, restated independently of the implementation."""
    if checkpoint:
        return False
    if stop == "abandoned":
        return True
    return stage in (OPEN_PR_STAGE, LEGACY_FINISHED_STAGE) and status == "complete" and stop == "none"


_ROWS = [
    (cp, stop, stage, status, gf)
    for cp in (False, True)
    for stop in ("none", "error", "abandoned")
    for stage, status in (
        (OPEN_PR_STAGE, "complete"),
        (LEGACY_FINISHED_STAGE, "complete"),
        (OPEN_PR_STAGE, "in_progress"),
        ("build", "in_progress"),
    )
    for gf in (0, 1)
]


def test_status_archive_and_run_agree_on_settledness(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    expected: dict[str, bool] = {}
    paths: dict[str, Path] = {}
    for i, (cp, stop, stage, status, gf) in enumerate(_ROWS):
        item = f"R-{i}"
        rec = RunRecord.open(ws, item)
        rec.mark_stage(stage, status)
        rec.set_checkpoint("C1" if cp else None)
        rec.retries[GATE_FAILED_KEY] = gf
        rec.save()
        if stop == "error":
            write_stop_record(
                rec.path,
                build_stop_record(
                    item, checkpoint=None, stage=stage, stage_status=status, detail="x", resume_command="r"
                ),
            )
        elif stop == "abandoned":
            write_stop_record(
                rec.path,
                build_abandoned_record(item, stage=stage, stage_status=status, prior_label="l", reenable_command="r"),
            )
        expected[item] = _rule(cp, stop, stage, status, gf)
        paths[item] = rec.path
    unsettled = {item for item, settled in expected.items() if not settled}
    assert unsettled and unsettled != set(expected)

    # _Run.settled() reads abandonment from ``was_abandoned``, since execute() clears the stop record.
    for i, (_cp, stop, *_rest) in enumerate(_ROWS):
        item = f"R-{i}"
        run = object.__new__(_Run)
        run.rec = RunRecord.open(ws, item)
        run.was_abandoned = stop == "abandoned"
        assert run.settled() == expected[item], item

    status_out = env.invoke("status", "--config", str(env.config))
    assert status_out.exit_code == 0, status_out.output
    shown = {item for item in expected if f"{item} " in status_out.output or f"{item}\n" in status_out.output}
    assert shown == unsettled

    archived = env.invoke("archive", "--config", str(env.config))
    assert archived.exit_code == 0, archived.output
    assert {item for item, path in paths.items() if path.exists()} == unsettled


def test_is_settled_abandoned_override(tmp_path: Path) -> None:
    rec = _rec(tmp_path, "build", "in_progress")
    rec.retries[GATE_FAILED_KEY] = 1
    assert is_settled(rec, None, abandoned=True)
    assert not is_settled(rec, None, abandoned=False)
    assert not is_settled(rec, None)


def test_is_settled_with_the_refactor_stage(tmp_path: Path) -> None:
    assert not is_settled(_rec(tmp_path, "refactor"), None)  # between build and push: not finished
    assert not is_settled(_rec(tmp_path, "refactor", "in_progress"), None)
    assert is_settled(_rec(tmp_path, OPEN_PR_STAGE), None)
