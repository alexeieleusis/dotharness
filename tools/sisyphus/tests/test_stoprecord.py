"""The stage-aware C5 action text (``action_for`` / ``build_stop_record``)."""

from __future__ import annotations

from sisyphus.stoprecord import ACTIONS, action_for, build_stop_record

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
