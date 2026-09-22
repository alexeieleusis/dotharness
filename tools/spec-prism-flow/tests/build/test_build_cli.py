"""Tests for the `build run`/`build status` CLI commands."""

from dataclasses import replace
from unittest.mock import Mock

from click.testing import CliRunner
from conftest import REPO, fake_phase_run_result, write_completion_log
from conftest import make_completion_record as _record
from conftest import make_config as _config
from conftest import make_phase as _phase
from conftest import write_phase_file as _write_phase_file

from spec_prism_flow import cli
from spec_prism_flow.build.agent_runner import branch_name
from spec_prism_flow.build.completion_log import COMPLETION_LOG_JSON_RELPATH
from spec_prism_flow.build.errors import EmptyImplementationError
from spec_prism_flow.build.resume_state import resume_state_path
from spec_prism_flow.config import BuildConfig


def _install_config(monkeypatch, cfg):
    monkeypatch.setattr(cli, "_load_cfg_or_raise", lambda config_path_str: cfg)


# --- build run: sequential/parallel dispatch ----------------------------------------


def test_build_run_sequential_dispatches_to_run_track(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path, build=BuildConfig(workers=1, state_dir=tmp_path / "state"))
    _install_config(monkeypatch, cfg)
    run_track = Mock(return_value=[fake_phase_run_result(1)])
    run_parallel = Mock()
    monkeypatch.setattr(cli.track_runner, "run_track", run_track)
    monkeypatch.setattr(cli.parallel_runner, "run_parallel", run_parallel)

    result = CliRunner().invoke(cli.cli, ["build", "run", "--start", "1", "--stop", "5"])

    assert result.exit_code == 0, result.output
    run_track.assert_called_once_with(
        cfg, tmp_path, start_phase=1, stop_phase=5, dry_run=False, resume=False, strict=False
    )
    run_parallel.assert_not_called()
    assert "Phase 01: merged" in result.output


def test_build_run_parallel_dispatches_to_run_parallel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path, build=BuildConfig(workers=3, state_dir=tmp_path / "state"))
    _install_config(monkeypatch, cfg)
    run_parallel = Mock(return_value=[fake_phase_run_result(2, merged=False)])
    run_track = Mock()
    monkeypatch.setattr(cli.parallel_runner, "run_parallel", run_parallel)
    monkeypatch.setattr(cli.track_runner, "run_track", run_track)

    result = CliRunner().invoke(cli.cli, ["build", "run", "--dry-run", "--strict"])

    assert result.exit_code == 0, result.output
    run_parallel.assert_called_once_with(cfg, tmp_path, workers=3, dry_run=True, strict=True)
    run_track.assert_not_called()
    assert "Phase 02: not merged" in result.output


def test_build_run_rejects_non_positive_workers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path, build=BuildConfig(workers=0, state_dir=tmp_path / "state"))
    _install_config(monkeypatch, cfg)

    result = CliRunner().invoke(cli.cli, ["build", "run"])

    assert result.exit_code != 0
    assert "workers" in result.output
    assert "0" in result.output


def test_build_run_rejects_start_stop_in_parallel_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path, build=BuildConfig(workers=2, state_dir=tmp_path / "state"))
    _install_config(monkeypatch, cfg)

    result = CliRunner().invoke(cli.cli, ["build", "run", "--start", "3", "--stop", "5"])

    assert result.exit_code != 0
    assert "--start" in result.output
    assert "--stop" in result.output


def test_build_run_exits_with_orchestration_error_exit_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path, build=BuildConfig(workers=1, state_dir=tmp_path / "state"))
    _install_config(monkeypatch, cfg)
    monkeypatch.setattr(cli.track_runner, "run_track", Mock(side_effect=EmptyImplementationError()))

    result = CliRunner().invoke(cli.cli, ["build", "run"])

    assert result.exit_code == EmptyImplementationError.exit_code
    assert "No implementation was produced" in result.output


# --- build status --------------------------------------------------------------------


def test_build_status_classifies_merged_escalated_pending_and_in_progress(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path, build=BuildConfig(workers=1, state_dir=tmp_path / "state"))
    _install_config(monkeypatch, cfg)

    phases = [_phase(number=n, name=f"leaf-{n}") for n in (1, 2, 3)]
    for phase in phases:
        _write_phase_file(cfg.plan.phase_dir, phase)

    records = [
        _record(phase_number=1, phase_name="leaf-1"),
        replace(_record(phase_number=2, phase_name="leaf-2"), pr_merged_at=None, escalation_reason="boom"),
    ]
    write_completion_log(tmp_path / COMPLETION_LOG_JSON_RELPATH, records)

    result = CliRunner().invoke(cli.cli, ["build", "status"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(line.startswith("01") and "merged" in line for line in lines)
    assert any(line.startswith("02") and "escalated" in line for line in lines)
    assert any(line.startswith("03") and "pending" in line for line in lines)

    state_path = resume_state_path(cfg, REPO, branch_name(phases[2]))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}")

    result_in_progress = CliRunner().invoke(cli.cli, ["build", "status"])
    assert any(line.startswith("03") and "in progress" in line for line in result_in_progress.output.splitlines())

    state_path.unlink()
    result_pending_again = CliRunner().invoke(cli.cli, ["build", "status"])
    assert any(line.startswith("03") and "pending" in line for line in result_pending_again.output.splitlines())


def test_build_status_prints_address_comments_cycles_and_escalations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path, build=BuildConfig(workers=1, state_dir=tmp_path / "state"))
    _install_config(monkeypatch, cfg)
    phase = _phase(number=1, name="only")
    _write_phase_file(cfg.plan.phase_dir, phase)
    write_completion_log(
        tmp_path / COMPLETION_LOG_JSON_RELPATH,
        [_record(phase_number=1, phase_name="only", address_comments_cycles=4, human_escalations=2)],
    )

    result = CliRunner().invoke(cli.cli, ["build", "status"])

    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("01"))
    assert "4" in row
    assert "2" in row


def test_no_rich_or_typer_imports_in_phase_12_modules():
    import re

    import spec_prism_flow.build.parallel_runner as parallel_runner_module
    import spec_prism_flow.build.track_runner as track_runner_module
    import spec_prism_flow.cli as cli_module

    for module in (cli_module, track_runner_module, parallel_runner_module):
        source = module.__file__
        assert source is not None
        text = open(source).read()  # noqa: SIM115
        assert not re.search(r"^\s*(?:import|from)\s+(rich|typer)\b", text, re.MULTILINE)
