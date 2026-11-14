"""The stage-aware C5 action text (``action_for`` / ``build_stop_record``)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dotharness.sisyphus.stoprecord import (
    ACTIONS,
    StopRecord,
    action_for,
    build_stop_record,
    clear_stop_record,
    read_stop_text,
    remove_stop_text,
    stop_record_path,
    stop_text_path,
    write_stop_record,
    write_stop_text,
)

MSG = "rename failed"
RESUME = "sisyphus resume ENG-1 --config /c.toml"


def _assert_intake_text(text: str) -> None:
    assert "The intake agent changed the target repository" in text
    assert "Inspect the paths listed in the detail" in text
    assert "remove or revert them by hand" in text
    assert f"`{RESUME}`" in text
    assert "No retry reset is needed" in text
    assert "{resume}" not in text
    assert "retry budget" not in text
    assert "--reset-retries" not in text


def test_action_for_c5_intake_is_specific() -> None:
    _assert_intake_text(action_for("C5", stage="intake", resume_command=RESUME))


def test_build_stop_record_c5_intake_action() -> None:
    stop = build_stop_record(
        "ENG-1",
        checkpoint="C5",
        stage="intake",
        stage_status="in_progress",
        detail="added: .venv/pyvenv.cfg",
        resume_command=RESUME,
    )
    _assert_intake_text(stop.action)
    assert stop.detail == "added: .venv/pyvenv.cfg"


def test_action_for_c5_without_intake_stage_is_unchanged() -> None:
    expected = ACTIONS["C5"].replace("{resume}", "sisyphus resume REF --config F")
    assert action_for("C5") == expected
    assert action_for("C5", stage="build") == expected
    assert action_for("C5", stage=None) == expected


def test_intake_stage_does_not_change_other_checkpoints() -> None:
    for checkpoint in ("C1", "C2", "C3", "C4", "C6", "C7", "C8"):
        assert action_for(checkpoint, stage="intake") == action_for(checkpoint)


def _stop(detail: str = "raw detail") -> StopRecord:
    return build_stop_record(
        "#204", checkpoint="C5", stage="build", stage_status="in_progress", detail=detail, resume_command=RESUME
    )


def test_stop_text_path_is_next_to_record_with_answer_stem(tmp_path: Path) -> None:
    assert stop_text_path(tmp_path / "_23204.json") == tmp_path / "_23204.stopped.md"


def test_stop_text_roundtrip_and_absent(tmp_path: Path) -> None:
    rec = tmp_path / "_1.json"
    assert read_stop_text(rec) is None
    write_stop_text(rec, "hello\n")
    assert read_stop_text(rec) == "hello\n"
    assert [p.name for p in tmp_path.iterdir()] == ["_1.stopped.md"]


def test_atomic_writer_leaves_no_partial_file_when_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = tmp_path / "_1.json"
    write_stop_text(rec, "old")

    def boom(src: object, dst: object) -> None:
        raise OSError(MSG)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match=MSG):
        write_stop_text(rec, "new")
    monkeypatch.undo()
    assert read_stop_text(rec) == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["_1.stopped.md"]


def test_atomic_writer_failure_on_fresh_file_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = tmp_path / "_1.json"

    def boom(src: object, dst: object) -> None:
        raise OSError(MSG)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match=MSG):
        write_stop_text(rec, "new")
    assert list(tmp_path.iterdir()) == []
    assert read_stop_text(rec) is None


def test_clear_stop_record_removes_stop_text(tmp_path: Path) -> None:
    rec = tmp_path / "_1.json"
    write_stop_record(rec, _stop())
    write_stop_text(rec, "text")
    clear_stop_record(rec)
    assert not stop_record_path(rec).exists()
    assert read_stop_text(rec) is None


def test_write_stop_record_removes_stop_text(tmp_path: Path) -> None:
    rec = tmp_path / "_1.json"
    write_stop_record(rec, _stop("first"))
    write_stop_text(rec, "text about first")
    write_stop_record(rec, _stop("second"))
    assert read_stop_text(rec) is None
    assert stop_record_path(rec).exists()


def test_stop_record_bytes_identical_with_and_without_stop_text(tmp_path: Path) -> None:
    stop = _stop()
    plain = tmp_path / "a" / "_1.json"
    with_text = tmp_path / "b" / "_1.json"
    write_stop_record(plain, stop)
    write_stop_record(with_text, stop)
    write_stop_text(with_text, "restructured")
    assert stop_record_path(plain).read_bytes() == stop_record_path(with_text).read_bytes()


def test_removal_without_file_does_not_raise(tmp_path: Path) -> None:
    rec = tmp_path / "_1.json"
    remove_stop_text(rec)
    clear_stop_record(rec)
    write_stop_record(rec, _stop())
    assert read_stop_text(rec) is None
