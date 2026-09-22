"""Tests for `discover_phase_files`/`already_merged_phase_numbers`/`run_track`."""

from unittest.mock import Mock

import pytest
from conftest import make_completion_record as _record
from conftest import make_config as _config
from conftest import make_phase as _phase
from conftest import write_completion_log
from conftest import write_phase_file as _write_phase_file

from spec_prism_flow.build import phase_runner, track_runner
from spec_prism_flow.build.errors import EmptyImplementationError
from spec_prism_flow.build.phase_runner import PhaseRunResult
from spec_prism_flow.build.track_runner import (
    already_merged_phase_numbers,
    discover_phase_files,
    run_track,
)

# --- discover_phase_files --------------------------------------------------------


def test_discover_phase_files_sorts_numerically_not_lexicographically(tmp_path):
    phases_dir = tmp_path / "phases"
    for phase in [_phase(number=2, name="two"), _phase(number=9, name="nine"), _phase(number=10, name="ten")]:
        _write_phase_file(phases_dir, phase)

    paths = discover_phase_files(phases_dir)

    assert [p.name for p in paths] == ["02-two-leaf.md", "09-nine-leaf.md", "10-ten-leaf.md"]


def test_discover_phase_files_ignores_non_matching_names(tmp_path):
    phases_dir = tmp_path / "phases"
    _write_phase_file(phases_dir, _phase(number=1, name="one"))
    (phases_dir / "graph.json").write_text("{}")
    (phases_dir / "AB-not-numbered-leaf.md").write_text("not a phase file")
    (phases_dir / "notes.md").write_text("not a leaf file")

    paths = discover_phase_files(phases_dir)

    assert [p.name for p in paths] == ["01-one-leaf.md"]


def test_discover_phase_files_empty_dir_returns_empty_list(tmp_path):
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    assert discover_phase_files(phases_dir) == []


# --- already_merged_phase_numbers ------------------------------------------------


def test_already_merged_phase_numbers_returns_empty_set_when_log_missing(tmp_path):
    assert already_merged_phase_numbers(tmp_path / "docs" / "completion-log.json") == set()


def test_already_merged_phase_numbers_returns_merged_only(tmp_path):
    log_path = tmp_path / "docs" / "completion-log.json"
    write_completion_log(
        log_path,
        [
            _record(phase_number=1, pr_merged_at=None, escalation_reason="boom"),
            _record(phase_number=2),
            _record(phase_number=3),
        ],
    )

    assert already_merged_phase_numbers(log_path) == {2, 3}


# --- run_track ---------------------------------------------------------------------


def _fake_result(number: int) -> PhaseRunResult:
    return PhaseRunResult(phase_number=number, merged=True, completion_record=_record(phase_number=number))


def test_run_track_skips_out_of_bounds_and_already_merged_phases(tmp_path, monkeypatch):
    config = _config(tmp_path)
    for number in (1, 2, 3, 4, 5):
        _write_phase_file(config.plan.phase_dir, _phase(number=number, name=f"leaf-{number}"))
    clone = tmp_path / "clone"
    write_completion_log(clone / track_runner.COMPLETION_LOG_JSON_RELPATH, [_record(phase_number=2)])

    run_phase = Mock(side_effect=lambda clone, config, phase, **kwargs: _fake_result(phase.number))
    monkeypatch.setattr(phase_runner, "run_phase", run_phase)

    results = run_track(config, clone, start_phase=1, stop_phase=4)

    called_numbers = [call.args[2].number for call in run_phase.call_args_list]
    assert called_numbers == [1, 3, 4]
    assert [r.phase_number for r in results] == [1, 3, 4]


def test_run_track_passes_dry_run_resume_strict_through(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_phase_file(config.plan.phase_dir, _phase(number=1, name="only"))
    clone = tmp_path / "clone"

    run_phase = Mock(return_value=_fake_result(1))
    monkeypatch.setattr(phase_runner, "run_phase", run_phase)

    run_track(config, clone, dry_run=True, resume=True, strict=True)

    run_phase.assert_called_once()
    args, kwargs = run_phase.call_args
    assert args[0] is clone
    assert args[1] is config
    assert args[2].number == 1
    assert kwargs == {"dry_run": True, "resume": True, "strict": True}


def test_run_track_propagates_orchestration_error_annotated_with_phase_context(tmp_path, monkeypatch):
    config = _config(tmp_path)
    phases = [_phase(number=n, name=f"leaf-{n}") for n in (1, 2, 3)]
    for phase in phases:
        _write_phase_file(config.plan.phase_dir, phase)
    clone = tmp_path / "clone"

    def _run_phase(clone, config, phase, **kwargs):
        if phase.number == 2:
            raise EmptyImplementationError()
        return _fake_result(phase.number)

    run_phase = Mock(side_effect=_run_phase)
    monkeypatch.setattr(phase_runner, "run_phase", run_phase)

    with pytest.raises(EmptyImplementationError) as exc_info:
        run_track(config, clone)

    assert exc_info.value.phase_number == 2
    assert exc_info.value.phase_name == "leaf-2"
    called_numbers = [call.args[2].number for call in run_phase.call_args_list]
    assert called_numbers == [1, 2]  # phase 3 never attempted after phase 2 escalates


def test_run_track_returns_empty_list_when_all_phases_already_merged(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_phase_file(config.plan.phase_dir, _phase(number=1, name="only"))
    clone = tmp_path / "clone"
    write_completion_log(clone / track_runner.COMPLETION_LOG_JSON_RELPATH, [_record(phase_number=1)])

    run_phase = Mock()
    monkeypatch.setattr(phase_runner, "run_phase", run_phase)

    assert run_track(config, clone) == []
    run_phase.assert_not_called()
