import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from spec_prism_flow.build import vibe_heal_integration
from spec_prism_flow.build.vibe_heal_integration import (
    Fingerprint,
    SonarScannerNotFoundError,
    VibeHealCommandError,
)
from spec_prism_flow.config import VibeHealConfig


def _config(*, enabled: bool = True) -> VibeHealConfig:
    return VibeHealConfig(enabled=enabled, command="vibe-heal", tool_dir=Path("/vendor/vibe-heal"))


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def _report_text() -> str:
    return json.dumps({
        "issues": [
            {"rule": "R1", "file": "a.py", "line": 1, "message": "m1", "on_changed_line": True},
            {"rule": "R2", "file": "b.py", "line": 2, "message": "m2", "on_changed_line": False},
        ]
    })


def test_scan_passes_explicit_report_file_and_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", Mock(return_value="/usr/local/bin/sonar-scanner"))
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)
    report_path = tmp_path / "report.json"
    report_path.write_text(_report_text())
    env_path = tmp_path / "vibe-heal.env"

    report = vibe_heal_integration.scan(_config(), tmp_path, report_path, env_path)

    argv = run_mock.call_args.args[0]
    assert argv == [
        "uv",
        "run",
        "--project",
        "/vendor/vibe-heal",
        "vibe-heal",
        "review",
        "--report-file",
        str(report_path),
        "--env-file",
        str(env_path),
    ]
    assert run_mock.call_args.kwargs["cwd"] == tmp_path
    assert report is not None
    assert len(report["issues"]) == 2


def test_scan_returns_none_and_makes_no_subprocess_call_when_disabled(tmp_path, monkeypatch):
    run_mock = Mock()
    monkeypatch.setattr("subprocess.run", run_mock)

    result = vibe_heal_integration.scan(_config(enabled=False), tmp_path, tmp_path / "r.json", tmp_path / "e.env")

    assert result is None
    run_mock.assert_not_called()


def test_scan_raises_sonar_scanner_not_found_error_when_missing_from_path(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", Mock(return_value=None))
    run_mock = Mock()
    monkeypatch.setattr("subprocess.run", run_mock)

    with pytest.raises(SonarScannerNotFoundError):
        vibe_heal_integration.scan(_config(), tmp_path, tmp_path / "r.json", tmp_path / "e.env")

    run_mock.assert_not_called()


def test_scan_raises_vibe_heal_command_error_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", Mock(return_value="/usr/local/bin/sonar-scanner"))
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="boom")))

    with pytest.raises(VibeHealCommandError) as exc_info:
        vibe_heal_integration.scan(_config(), tmp_path, tmp_path / "r.json", tmp_path / "e.env")

    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "boom"


def test_scan_raises_vibe_heal_command_error_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", Mock(return_value="/usr/local/bin/sonar-scanner"))
    monkeypatch.setattr("subprocess.run", Mock(side_effect=subprocess.TimeoutExpired(cmd=["vibe-heal"], timeout=1800)))

    with pytest.raises(VibeHealCommandError) as exc_info:
        vibe_heal_integration.scan(_config(), tmp_path, tmp_path / "r.json", tmp_path / "e.env")

    assert "timed out after 1800s" in exc_info.value.stderr


def test_post_passes_explicit_report_file_env_file_and_post_flag(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)
    report_path = tmp_path / "report.json"
    env_path = tmp_path / "vibe-heal.env"

    vibe_heal_integration.post(_config(), tmp_path, report_path, env_path)

    argv = run_mock.call_args.args[0]
    assert argv == [
        "uv",
        "run",
        "--project",
        "/vendor/vibe-heal",
        "vibe-heal",
        "review",
        "--report-file",
        str(report_path),
        "--env-file",
        str(env_path),
        "--post",
    ]


def test_post_is_noop_and_makes_no_subprocess_call_when_disabled(tmp_path, monkeypatch):
    run_mock = Mock()
    monkeypatch.setattr("subprocess.run", run_mock)

    result = vibe_heal_integration.post(_config(enabled=False), tmp_path, tmp_path / "r.json", tmp_path / "e.env")

    assert result is None
    run_mock.assert_not_called()


def test_fingerprints_from_report_includes_only_on_changed_line_issues():
    report = json.loads(_report_text())

    fingerprints = vibe_heal_integration.fingerprints_from_report(report)

    assert fingerprints == {Fingerprint(rule="R1", file="a.py", line=1)}


def test_fingerprints_from_report_returns_empty_set_for_no_issues():
    assert vibe_heal_integration.fingerprints_from_report({"issues": []}) == set()


def test_should_post_true_when_current_has_a_fingerprint_never_posted_before():
    current = {Fingerprint(rule="R1", file="a.py", line=1), Fingerprint(rule="R2", file="b.py", line=2)}
    already_posted = {Fingerprint(rule="R1", file="a.py", line=1)}

    assert vibe_heal_integration.should_post(current, already_posted) is True


def test_should_post_false_when_current_is_a_subset_of_already_posted():
    current = {Fingerprint(rule="R1", file="a.py", line=1)}
    already_posted = {Fingerprint(rule="R1", file="a.py", line=1), Fingerprint(rule="R2", file="b.py", line=2)}

    assert vibe_heal_integration.should_post(current, already_posted) is False


def test_should_post_false_when_current_is_empty():
    assert vibe_heal_integration.should_post(set(), {Fingerprint(rule="R1", file="a.py", line=1)}) is False


def test_diff_fingerprints_counts_opened_and_resolved():
    previous = {Fingerprint(rule="R1", file="a.py", line=1), Fingerprint(rule="R2", file="b.py", line=2)}
    current = {Fingerprint(rule="R2", file="b.py", line=2), Fingerprint(rule="R3", file="c.py", line=3)}

    opened, resolved = vibe_heal_integration.diff_fingerprints(previous, current)

    assert opened == 1
    assert resolved == 1


def test_diff_fingerprints_no_change():
    fingerprints = {Fingerprint(rule="R1", file="a.py", line=1)}

    assert vibe_heal_integration.diff_fingerprints(fingerprints, fingerprints) == (0, 0)
