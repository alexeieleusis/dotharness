from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

from sisyphus import runrecord
from sisyphus.runrecord import (
    DEFAULT_STATE_DIR,
    STAGE_COMPLETE,
    STAGE_IN_PROGRESS,
    RunRecord,
    RunRecordError,
    encode_item,
    record_path,
)


def test_default_state_dir() -> None:
    assert DEFAULT_STATE_DIR == "~/.local/share/dotharness/sisyphus"


def test_create_writes_all_fields(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "ENG-1")
    data = json.loads(rec.path.read_text())
    assert data["item"] == "ENG-1"
    assert data["stage"] is None
    assert data["checkpoint"] is None
    assert data["started_at"]
    assert data["updated_at"]
    assert data["retries"] == {}


def test_creates_workspace_dir(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path / "a" / "b", "X")
    assert rec.path.exists()


def test_reload_reports_stage_and_separate_items(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X")
    rec.mark_stage("build", STAGE_COMPLETE)
    started = rec.started_at
    again = RunRecord.open(tmp_path, "X")
    assert again.stage == "build"
    assert again.stage_status == STAGE_COMPLETE
    assert again.started_at == started
    other = RunRecord.open(tmp_path, "Y")
    assert other.path != again.path
    assert other.stage is None


def test_stage_scope_marks_in_progress_then_complete(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X")
    with rec.stage_scope("review"):
        on_disk = RunRecord.open(tmp_path, "X")
        assert (on_disk.stage, on_disk.stage_status) == ("review", STAGE_IN_PROGRESS)
    on_disk = RunRecord.open(tmp_path, "X")
    assert (on_disk.stage, on_disk.stage_status) == ("review", STAGE_COMPLETE)


def test_crash_mid_stage_leaves_unfinished_stage(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X")
    with pytest.raises(RuntimeError), rec.stage_scope("review"):
        raise RuntimeError("boom")
    again = RunRecord.open(tmp_path, "X")
    assert (again.stage, again.stage_status) == ("review", STAGE_IN_PROGRESS)


def test_checkpoint_set_and_clear(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X")
    rec.set_checkpoint("C3")
    assert RunRecord.open(tmp_path, "X").checkpoint == "C3"
    rec.set_checkpoint(None)
    assert RunRecord.open(tmp_path, "X").checkpoint is None
    with pytest.raises(ValueError, match="checkpoint"):
        rec.set_checkpoint("")


def test_retries_survive_reload(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X")
    assert rec.increment_retry("ci-fix") == 1
    assert rec.increment_retry("ci-fix") == 2
    again = RunRecord.open(tmp_path, "X")
    assert again.retries == {"ci-fix": 2}
    assert again.increment_retry("ci-fix") == 3
    again.mark_stage("s", STAGE_COMPLETE)
    assert RunRecord.open(tmp_path, "X").retries == {"ci-fix": 3}


def test_interrupted_write_keeps_previous_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = RunRecord.open(tmp_path, "X")
    rec.mark_stage("one", STAGE_COMPLETE)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("interrupted before rename")  # noqa: TRY003

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="interrupted"):
        rec.mark_stage("two")
    monkeypatch.undo()
    loaded = RunRecord.open(tmp_path, "X")
    assert (loaded.stage, loaded.stage_status) == ("one", STAGE_COMPLETE)
    assert [p.name for p in tmp_path.iterdir()] == [rec.path.name]


@pytest.mark.parametrize("content", ["", '{"item": "X", "sta', "[]", "{}", "not json"])
def test_corrupt_record_raises_naming_file(tmp_path: Path, content: str) -> None:
    rec = RunRecord.open(tmp_path, "X")
    rec.path.write_text(content)
    with pytest.raises(RunRecordError) as err:
        RunRecord.open(tmp_path, "X")
    assert str(rec.path) in str(err.value)
    assert rec.path.read_text() == content


def test_invalid_field_values_rejected(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X")
    good = json.loads(rec.path.read_text())
    for patch in (
        {"item": "other"},
        {"stage_status": "weird"},
        {"retries": {"a": -1}},
        {"retries": {"a": True}},
        {"retries": []},
        {"stage": 3},
        {"started_at": 1},
    ):
        rec.path.write_text(json.dumps({**good, **patch}))
        with pytest.raises(RunRecordError, match="invalid"):
            RunRecord.open(tmp_path, "X")


def test_encoding_deterministic_safe_and_collision_free() -> None:
    refs = ["a", "A", "a/b", "a_b", "a_2fb", "..", ".", "a b", "é", "ENG-1", "eng-1", "x" * 500, "x" * 501, "a\\b"]
    names = [encode_item(r) for r in refs]
    assert len(set(names)) == len(refs)
    assert names == [encode_item(r) for r in refs]
    for name in names:
        assert name not in {".", ".."}
        assert re.fullmatch(r"[a-z0-9_-]+", name)
        assert len(name) < 200


def test_encoding_rejects_empty() -> None:
    with pytest.raises(ValueError, match="item"):
        encode_item("")


def test_odd_references_get_distinct_files(tmp_path: Path) -> None:
    a = RunRecord.open(tmp_path, "../evil")
    b = RunRecord.open(tmp_path, "ENG-1")
    c = RunRecord.open(tmp_path, "eng-1")
    assert {p.parent for p in (a.path, b.path, c.path)} == {tmp_path}
    assert len({a.path, b.path, c.path}) == 3


def test_record_path_is_verbatim(tmp_path: Path) -> None:
    assert record_path(tmp_path, "X") == tmp_path / "_58.json"


def test_module_has_no_forbidden_imports() -> None:
    tree = ast.parse(Path(runrecord.__file__).read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert not {m for m in mods if "config" in m or "sisyphus" in m or "harness" in m or "spec_prism_flow" in m}
