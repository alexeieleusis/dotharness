import subprocess
from unittest.mock import Mock, call

import pytest
from conftest import git_commit as _commit
from conftest import init_git_repo as _init_repo
from conftest import run_git as _run_git

from spec_prism_flow.build import git_ops
from spec_prism_flow.build.git_ops import DiffStat, GitCommandError


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


def test_commit_all_raises_on_commit_failure_after_dirty_tree_detected(tmp_path, monkeypatch):
    responses = [_ok(), _ok(stdout=" M file.py"), Mock(returncode=1, stdout="", stderr="pre-commit hook failed")]
    run_mock = Mock(side_effect=responses)
    monkeypatch.setattr("subprocess.run", run_mock)

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.commit_all(tmp_path, "phase 07: x")

    assert run_mock.call_count == 3
    assert exc_info.value.cmd_args == ["git", "commit", "-m", "phase 07: x", "--"]
    assert exc_info.value.stderr == "pre-commit hook failed"


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


def test_push_branch_with_expect_sha_pins_the_lease_and_skips_the_refresh_fetch(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.push_branch(tmp_path, "phase-07-x", expect_sha="deadbeef")

    assert run_mock.call_count == 1
    assert run_mock.call_args.args[0] == [
        "git",
        "push",
        "--force-with-lease=phase-07-x:deadbeef",
        "-u",
        "origin",
        "phase-07-x",
    ]


def test_push_branch_with_expect_sha_raises_git_command_error_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="stale info")))

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.push_branch(tmp_path, "phase-07-x", expect_sha="deadbeef")

    assert exc_info.value.stderr == "stale info"


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


def test_discard_working_tree_changes_resets_then_cleans(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    git_ops.discard_working_tree_changes(tmp_path)

    assert run_mock.call_args_list == [
        call(
            ["git", "reset", "--hard", "HEAD"],
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


def test_discard_working_tree_changes_raises_git_command_error_on_reset_failure(tmp_path, monkeypatch):
    run_mock = Mock(return_value=Mock(returncode=1, stdout="", stderr="fatal: not a git repository"))
    monkeypatch.setattr("subprocess.run", run_mock)

    with pytest.raises(GitCommandError) as exc_info:
        git_ops.discard_working_tree_changes(tmp_path)

    assert exc_info.value.cmd_args == ["git", "reset", "--hard", "HEAD"]
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


# --- diff_stat: real scratch git repo ------------------------------------------------


def test_diff_stat_counts_files_and_lines_added_and_removed(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\nline2\n")
    _commit(clone, "initial")

    (clone / "a.txt").write_text("line1\nline2-changed\nline3\n")
    (clone / "b.txt").write_text("new file\n")
    _commit(clone, "second")

    stat = git_ops.diff_stat(clone, "HEAD~1")

    assert stat.files == 2
    assert stat.lines_added == 3  # 1 changed line (add) + 1 new line in a.txt + 1 line in b.txt
    assert stat.lines_removed == 1  # the replaced line in a.txt


def test_diff_stat_returns_zero_when_no_changes(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")

    stat = git_ops.diff_stat(clone, "HEAD")

    assert stat == DiffStat(files=0, lines_added=0, lines_removed=0)


def test_diff_stat_counts_binary_files_without_line_counts(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")

    (clone / "binary.dat").write_bytes(b"\x00\x01\x02binary")
    _commit(clone, "add binary")

    stat = git_ops.diff_stat(clone, "HEAD~1")

    assert stat.files == 1
    assert stat.lines_added == 0
    assert stat.lines_removed == 0


def test_diff_stat_raises_git_command_error_for_unknown_base_ref(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")

    with pytest.raises(GitCommandError):
        git_ops.diff_stat(clone, "nonexistent-ref")


# --- add_worktree/remove_worktree: real scratch git repo --------------------------


def test_add_worktree_creates_an_isolated_checkout_of_clones_head(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")
    worktree_path = tmp_path / "worktrees" / "phase-01-a"

    git_ops.add_worktree(clone, worktree_path)

    assert (worktree_path / "a.txt").read_text() == "line1\n"
    assert (worktree_path / ".git").exists()


def test_add_worktree_lets_two_worktrees_hold_different_branches_concurrently(tmp_path):
    """The bug this fix closes: before per-phase worktrees, two concurrently-running
    phases shared one working tree, so one phase's `checkout -B` reset the branch out
    from under the other. Two worktrees off the same clone must be able to check out
    distinct branches at once without either stepping on the other."""
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")
    wt1 = tmp_path / "worktrees" / "phase-01-a"
    wt2 = tmp_path / "worktrees" / "phase-02-b"

    git_ops.add_worktree(clone, wt1)
    git_ops.add_worktree(clone, wt2)
    _run_git(wt1, "checkout", "-b", "phase-01-a")
    _run_git(wt2, "checkout", "-b", "phase-02-b")
    (wt1 / "a.txt").write_text("from phase 1\n")
    (wt2 / "a.txt").write_text("from phase 2\n")

    assert (wt1 / "a.txt").read_text() == "from phase 1\n"
    assert (wt2 / "a.txt").read_text() == "from phase 2\n"


def test_add_worktree_replaces_a_stale_worktree_left_by_an_abandoned_run(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")
    worktree_path = tmp_path / "worktrees" / "phase-01-a"
    git_ops.add_worktree(clone, worktree_path)
    (worktree_path / "leftover.txt").write_text("scratch from an abandoned run\n")

    git_ops.add_worktree(clone, worktree_path)

    assert not (worktree_path / "leftover.txt").exists()


def test_remove_worktree_deletes_the_directory(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")
    worktree_path = tmp_path / "worktrees" / "phase-01-a"
    git_ops.add_worktree(clone, worktree_path)

    git_ops.remove_worktree(clone, worktree_path)

    assert not worktree_path.exists()


def test_remove_worktree_force_removes_even_with_uncommitted_changes(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")
    worktree_path = tmp_path / "worktrees" / "phase-01-a"
    git_ops.add_worktree(clone, worktree_path)
    (worktree_path / "dirty.txt").write_text("uncommitted\n")

    git_ops.remove_worktree(clone, worktree_path)

    assert not worktree_path.exists()


def test_remove_worktree_is_a_noop_when_the_path_does_not_exist(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")

    git_ops.remove_worktree(clone, tmp_path / "worktrees" / "never-created")
