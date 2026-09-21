import subprocess
from unittest.mock import Mock, call

import pytest

from spec_prism_flow.build import git_ops
from spec_prism_flow.build.git_ops import GitCommandError


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def test_checkout_fresh_branch_fetches_then_checks_out_dash_b_from_origin(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.checkout_fresh_branch(tmp_path, "phase-07-x", "main")

    assert run_mock.call_args_list == [
        call(
            ["git", "fetch", "origin", "main"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=git_ops._GIT_TIMEOUT_SECONDS,
        ),
        call(
            ["git", "checkout", "-B", "phase-07-x", "origin/main"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=git_ops._GIT_TIMEOUT_SECONDS,
        ),
    ]


def test_checkout_fresh_branch_raises_git_command_error_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="no such remote")))

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.checkout_fresh_branch(tmp_path, "phase-07-x", "main")

    err = exc_info.value
    assert err.cmd_args == ["git", "fetch", "origin", "main"]
    assert err.returncode == 1
    assert err.stderr == "no such remote"


def test_commit_all_returns_false_and_does_not_commit_on_clean_tree(tmp_path, monkeypatch):
    responses = [_ok(), _ok(stdout="")]
    run_mock = Mock(side_effect=responses)
    monkeypatch.setattr("subprocess.run", run_mock)

    assert git_ops.commit_all(tmp_path, "phase 07: x") is False
    assert run_mock.call_count == 2
    assert [c.args[0][:2] for c in run_mock.call_args_list] == [["git", "add"], ["git", "status"]]


def test_commit_all_returns_true_and_commits_on_dirty_tree(tmp_path, monkeypatch):
    responses = [_ok(), _ok(stdout=" M file.py\n"), _ok()]
    run_mock = Mock(side_effect=responses)
    monkeypatch.setattr("subprocess.run", run_mock)

    assert git_ops.commit_all(tmp_path, "phase 07: x") is True
    assert run_mock.call_count == 3
    assert run_mock.call_args_list[2].args[0] == ["git", "commit", "-m", "phase 07: x", "--"]


def test_commit_all_raises_on_add_failure_without_checking_status(tmp_path, monkeypatch):
    run_mock = Mock(return_value=Mock(returncode=1, stdout="", stderr="fatal"))
    monkeypatch.setattr("subprocess.run", run_mock)

    with pytest.raises(GitCommandError):
        git_ops.commit_all(tmp_path, "phase 07: x")

    assert run_mock.call_count == 1


def test_push_branch_uses_force_with_lease(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.push_branch(tmp_path, "phase-07-x")

    assert run_mock.call_args.args[0] == ["git", "push", "--force-with-lease", "-u", "origin", "phase-07-x"]


def test_push_branch_raises_git_command_error_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="rejected")))

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.push_branch(tmp_path, "phase-07-x")

    assert exc_info.value.stderr == "rejected"


def test_delete_remote_branch_if_exists_pushes_delete_when_branch_present(tmp_path, monkeypatch):
    responses = [Mock(returncode=0, stdout="deadbeef\trefs/heads/phase-07-x\n", stderr=""), _ok()]
    run_mock = Mock(side_effect=responses)
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.delete_remote_branch_if_exists(tmp_path, "phase-07-x")

    assert run_mock.call_args_list[0].args[0] == ["git", "ls-remote", "--exit-code", "--heads", "origin", "phase-07-x"]
    assert run_mock.call_args_list[1].args[0] == ["git", "push", "origin", "--delete", "phase-07-x"]


def test_delete_remote_branch_if_exists_does_nothing_when_branch_absent(tmp_path, monkeypatch):
    run_mock = Mock(return_value=Mock(returncode=2, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.delete_remote_branch_if_exists(tmp_path, "phase-07-x")

    assert run_mock.call_count == 1


def test_delete_remote_branch_if_exists_raises_git_command_error_on_ls_remote_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=128, stdout="", stderr="no such remote")))

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.delete_remote_branch_if_exists(tmp_path, "phase-07-x")

    assert exc_info.value.stderr == "no such remote"


def test_fetch_resync_fetches_then_checks_out_dash_b_from_remote_branch(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.fetch_resync(tmp_path, "phase-07-x")

    assert run_mock.call_args_list == [
        call(
            ["git", "fetch", "origin", "phase-07-x"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=git_ops._GIT_TIMEOUT_SECONDS,
        ),
        call(
            ["git", "checkout", "-B", "phase-07-x", "origin/phase-07-x"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=git_ops._GIT_TIMEOUT_SECONDS,
        ),
    ]


def test_diff_name_only_filters_blank_lines(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=_ok(stdout="a.py\n\nb/c.py\n")))

    assert git_ops.diff_name_only(tmp_path) == ["a.py", "b/c.py"]


def test_diff_name_only_uses_configured_base_ref(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.diff_name_only(tmp_path, base_ref="origin/develop")

    assert run_mock.call_args.args[0] == ["git", "diff", "--name-only", "origin/develop...HEAD"]


def test_diff_name_only_raises_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=128, stdout="", stderr="bad ref")))

    with pytest.raises(GitCommandError):
        git_ops.diff_name_only(tmp_path)


def test_head_sha_returns_stripped_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=_ok(stdout="deadbeef\n")))

    assert git_ops.head_sha(tmp_path) == "deadbeef"


def test_head_sha_raises_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=128, stdout="", stderr="not a repo")))

    with pytest.raises(GitCommandError):
        git_ops.head_sha(tmp_path)


def test_discard_working_tree_changes_checks_out_then_cleans(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.discard_working_tree_changes(tmp_path)

    assert run_mock.call_args_list == [
        call(
            ["git", "checkout", "--", "."],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=git_ops._GIT_TIMEOUT_SECONDS,
        ),
        call(
            ["git", "clean", "-fd"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=git_ops._GIT_TIMEOUT_SECONDS,
        ),
    ]


def test_discard_working_tree_changes_raises_git_command_error_on_checkout_failure(tmp_path, monkeypatch):
    run_mock = Mock(return_value=Mock(returncode=1, stdout="", stderr="fatal: not a git repository"))
    monkeypatch.setattr("subprocess.run", run_mock)

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.discard_working_tree_changes(tmp_path)

    assert exc_info.value.cmd_args == ["git", "checkout", "--", "."]
    assert exc_info.value.stderr == "fatal: not a git repository"
    assert run_mock.call_count == 1


def test_run_raises_git_command_error_instead_of_hanging_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        Mock(side_effect=subprocess.TimeoutExpired(cmd=["git", "push"], timeout=git_ops._GIT_TIMEOUT_SECONDS)),
    )

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.push_branch(tmp_path, "phase-07-x")

    assert exc_info.value.cmd_args == ["git", "push", "--force-with-lease", "-u", "origin", "phase-07-x"]
    assert str(git_ops._GIT_TIMEOUT_SECONDS) in exc_info.value.stderr
