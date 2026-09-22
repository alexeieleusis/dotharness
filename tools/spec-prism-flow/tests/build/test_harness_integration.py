import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from spec_prism_flow.build import harness_integration
from spec_prism_flow.build.harness_integration import HarnessCommandError
from spec_prism_flow.config import ReviewConfig


def _config(*, enabled: bool = True) -> ReviewConfig:
    return ReviewConfig(
        enabled=enabled,
        command="harness",
        tool_dir=Path("/tools/pr-review"),
        harness_config=Path("/repo/.harness.toml"),
    )


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def test_run_invokes_uv_run_harness_with_config_and_subcommand(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok(stdout="review output"))
    monkeypatch.setattr("subprocess.run", run_mock)

    output = harness_integration.run(_config(), tmp_path, "self-review")

    assert output == "review output"
    assert run_mock.call_args.args[0] == [
        "uv",
        "run",
        "--project",
        "/tools/pr-review",
        "harness",
        "run",
        "--config",
        "/repo/.harness.toml",
        "self-review",
    ]
    assert run_mock.call_args.kwargs["cwd"] == tmp_path
    assert run_mock.call_args.kwargs["timeout"] == harness_integration.DEFAULT_HARNESS_TIMEOUT_SECONDS


def test_run_passes_through_caller_supplied_timeout(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    harness_integration.run(_config(), tmp_path, "self-review", timeout=45)

    assert run_mock.call_args.kwargs["timeout"] == 45


def test_run_raises_harness_command_error_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="boom")))

    with pytest.raises(HarnessCommandError) as exc_info:
        harness_integration.run(_config(), tmp_path, "self-review")

    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "boom"


def test_run_raises_harness_command_error_on_timeout(tmp_path, monkeypatch):
    cmd = [
        "uv",
        "run",
        "--project",
        "/tools/pr-review",
        "harness",
        "run",
        "--config",
        "/repo/.harness.toml",
        "self-review",
    ]
    monkeypatch.setattr("subprocess.run", Mock(side_effect=subprocess.TimeoutExpired(cmd=cmd, timeout=1800)))

    with pytest.raises(HarnessCommandError) as exc_info:
        harness_integration.run(_config(), tmp_path, "self-review")

    assert "timed out after 1800s" in exc_info.value.stderr


def test_self_review_delegates_to_run_with_self_review_subcommand(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok(stdout="ok"))
    monkeypatch.setattr("subprocess.run", run_mock)

    output = harness_integration.self_review(_config(), tmp_path)

    assert output == "ok"
    assert run_mock.call_args.args[0][-1] == "self-review"


def test_self_review_is_noop_and_makes_no_subprocess_call_when_disabled(tmp_path, monkeypatch):
    run_mock = Mock()
    monkeypatch.setattr("subprocess.run", run_mock)

    output = harness_integration.self_review(_config(enabled=False), tmp_path)

    assert output == ""
    run_mock.assert_not_called()


def test_address_comments_delegates_to_run_with_address_comments_subcommand(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok(stdout="ok"))
    monkeypatch.setattr("subprocess.run", run_mock)

    output = harness_integration.address_comments(_config(), tmp_path)

    assert output == "ok"
    assert run_mock.call_args.args[0][-1] == "address-comments"


def test_address_comments_is_noop_and_makes_no_subprocess_call_when_disabled(tmp_path, monkeypatch):
    run_mock = Mock()
    monkeypatch.setattr("subprocess.run", run_mock)

    output = harness_integration.address_comments(_config(enabled=False), tmp_path)

    assert output == ""
    run_mock.assert_not_called()
