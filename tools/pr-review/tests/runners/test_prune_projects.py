import subprocess
from unittest.mock import MagicMock, patch

from harness.config import HarnessConfig, HarnessSection, RepoConfig, SubDir, VibehealConfig
from harness.runners import prune_projects


def _make_config(tmp_path, subdirs, **vibe_heal_kwargs):
    return HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path, subdirs=subdirs),
        vibe_heal=VibehealConfig(enabled=True, python="/venv/bin/python3", **vibe_heal_kwargs),
    )


def test_noop_when_disabled(tmp_path):
    cfg = _make_config(tmp_path, [SubDir(path=".")], prune_projects_enabled=False)
    with patch("harness.runners.prune_projects.run_cmd") as mock_run:
        prune_projects.run(cfg, {})
    mock_run.assert_not_called()


def test_runs_prune_command_per_subdir(tmp_path):
    cfg = _make_config(
        tmp_path,
        [SubDir(path="a"), SubDir(path="b")],
        prune_projects_enabled=True,
        prune_older_than_minutes=90,
        prune_projects_timeout=45,
    )
    with patch("harness.runners.prune_projects.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        prune_projects.run(cfg, {"FOO": "bar"})
    assert mock_run.call_count == 2
    for call, expected_path in zip(mock_run.call_args_list, ["a", "b"], strict=True):
        cmd = call.args[0]
        assert cmd == [
            "/venv/bin/python3",
            "-m",
            "vibe_heal",
            "prune-projects",
            "--yes",
            "--older-than",
            "90",
        ]
        assert call.kwargs["cwd"] == str(tmp_path / expected_path)
        assert call.kwargs["env"] == {"FOO": "bar"}
        assert call.kwargs["timeout"] == 45


def test_no_op_with_no_subdirs(tmp_path):
    cfg = _make_config(tmp_path, [], prune_projects_enabled=True)
    with patch("harness.runners.prune_projects.run_cmd") as mock_run:
        prune_projects.run(cfg, {})
    mock_run.assert_not_called()


def test_failure_in_one_subdir_does_not_stop_the_next(tmp_path):
    cfg = _make_config(tmp_path, [SubDir(path="a"), SubDir(path="b")], prune_projects_enabled=True)
    with patch("harness.runners.prune_projects.run_cmd") as mock_run:
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "vibe_heal", output=b"out", stderr=b"err"),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]
        prune_projects.run(cfg, {})
    assert mock_run.call_count == 2


def test_timeout_is_caught_and_logged(tmp_path):
    cfg = _make_config(tmp_path, [SubDir(path=".")], prune_projects_enabled=True)
    with patch("harness.runners.prune_projects.run_cmd") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("vibe_heal", 45)
        prune_projects.run(cfg, {})
    mock_run.assert_called_once()


def test_os_error_in_one_subdir_does_not_stop_the_next(tmp_path):
    cfg = _make_config(tmp_path, [SubDir(path="a"), SubDir(path="b")], prune_projects_enabled=True)
    with patch("harness.runners.prune_projects.run_cmd") as mock_run:
        mock_run.side_effect = [
            FileNotFoundError("/venv/bin/python3"),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]
        prune_projects.run(cfg, {})
    assert mock_run.call_count == 2
