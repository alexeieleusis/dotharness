import os
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.code_host import ConfigurationError, RepositoryRef
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.pr_review.runners.common import (
    FatalGitError,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    local_identity_check,
    repo_identity_check,
    run_cmd,
)
from dotharness.repo_guard import RepoIdentityError


async def _run(cmd, tmp_path, **kw):
    kw.setdefault("timeout", 5)
    return await run_cmd(cmd, cwd=str(tmp_path), env=os.environ.copy(), **kw)


async def test_run_cmd_success_returns_bytes(tmp_path):
    result = await _run(["sh", "-c", "printf out; printf err >&2"], tmp_path)
    assert result.returncode == 0
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert isinstance(result.stdout, bytes)
    assert isinstance(result.stderr, bytes)


async def test_run_cmd_newline_preserved_in_bytes(tmp_path):
    assert (await _run(["sh", "-c", "printf 'a\\n'"], tmp_path)).stdout == b"a\n"


async def test_run_cmd_nonzero_raises(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as exc:
        await _run(["sh", "-c", "printf out; printf err >&2; exit 3"], tmp_path)
    assert exc.value.returncode == 3
    assert exc.value.stdout == b"out"
    assert exc.value.stderr == b"err"
    assert exc.value.cmd == ["sh", "-c", "printf out; printf err >&2; exit 3"]


async def test_run_cmd_nonzero_without_check_returns(tmp_path):
    result = await _run(["sh", "-c", "printf x; exit 2"], tmp_path, check=False)
    assert result.returncode == 2
    assert result.stdout == b"x"


async def test_run_cmd_timeout_raises_timeout_expired_with_bytes(tmp_path):
    with (
        patch("dotharness.subprocess_runner.KILL_GRACE_SECONDS", 1.0),
        pytest.raises(subprocess.TimeoutExpired) as exc,
    ):
        await _run(["sh", "-c", "printf partial; sleep 30"], tmp_path, timeout=1)
    assert exc.value.timeout == 1
    assert exc.value.cmd == ["sh", "-c", "printf partial; sleep 30"]
    assert exc.value.stdout in (None, b"partial")
    assert isinstance(exc.value.stderr, bytes)


async def test_run_cmd_timeout_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    with (
        patch("dotharness.subprocess_runner.KILL_GRACE_SECONDS", 1.0),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        await _run(["sh", "-c", script], tmp_path, timeout=1)
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("grandchild survived the timeout kill")


async def test_run_cmd_missing_executable_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        await _run(["definitely-not-a-real-binary-xyz"], tmp_path)


async def test_run_cmd_missing_cwd_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        await _run(["true"], tmp_path / "nope")


async def test_run_cmd_shell_string(tmp_path):
    result = await _run("echo a && echo b >&2", tmp_path, shell=True)  # noqa: S604 - exercises run_cmd shell mode
    assert result.stdout == b"a\n"
    assert result.stderr == b"b\n"


async def test_run_cmd_shell_missing_command_is_exit_127_not_exception(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as exc:
        await _run("definitely-not-a-real-binary-xyz", tmp_path, shell=True)  # noqa: S604 - exercises run_cmd shell mode
    assert exc.value.returncode == 127


async def test_run_cmd_passes_env_and_cwd(tmp_path):
    result = await run_cmd(
        ["sh", "-c", 'printf \'%s %s\' "$FOO" "$PWD"'], cwd=str(tmp_path), env={"FOO": "bar"}, timeout=5
    )
    assert result.stdout.startswith(b"bar ")


def test_run_cmd_has_no_process_code_of_its_own():
    import inspect

    from dotharness.pr_review.runners import common

    src = inspect.getsource(common)
    assert "killpg" not in src
    assert "start_new_session" not in src
    assert "Popen(" not in src


async def test_git_detach_records_sha(tmp_path):
    sha = "abc1234def5678"
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(stdout=f"{sha}\n".encode())
        result = await git_detach_and_record(str(tmp_path), {})
    assert result == sha


async def test_git_fetch_and_checkout_calls(tmp_path):
    with (
        patch("dotharness.pr_review.runners.common.git_clean_if_dirty"),
        patch("dotharness.pr_review.runners.common.run_cmd") as mock_run,
    ):
        await git_fetch_and_checkout("feature-branch", str(tmp_path), {})
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("fetch" in c for c in calls)
    assert any("checkout" in c and "feature-branch" in c for c in calls)


async def test_git_detach_raises_fatal_error_on_called_process_error(tmp_path):
    with (
        patch(
            "dotharness.pr_review.runners.common.run_cmd",
            side_effect=subprocess.CalledProcessError(1, ["git", "checkout"]),
        ),
        pytest.raises(FatalGitError),
    ):
        await git_detach_and_record(str(tmp_path), {})


async def test_git_fetch_and_checkout_raises_fatal_error_on_called_process_error(tmp_path):
    with (
        patch(
            "dotharness.pr_review.runners.common.run_cmd",
            side_effect=subprocess.CalledProcessError(1, ["git", "fetch"]),
        ),
        pytest.raises(FatalGitError),
    ):
        await git_fetch_and_checkout("feature-branch", str(tmp_path), {})


def _git_restore_side_effect(is_ancestor_returncode):
    def side_effect(cmd, **kwargs):
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"deadbeef1234\n", stderr=b"")
        if "merge-base" in cmd:
            return MagicMock(returncode=is_ancestor_returncode, stdout=b"", stderr=b"")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    return side_effect


async def test_git_restore_preserves_commits_not_on_origin(tmp_path, caplog):
    mock_run = AsyncMock(side_effect=_git_restore_side_effect(is_ancestor_returncode=1))
    with patch("dotharness.pr_review.runners.common.run_cmd", mock_run), caplog.at_level("WARNING"):
        await git_restore("orig-sha", "my-branch", str(tmp_path), {})
    update_ref_calls = [c for c in mock_run.call_args_list if c.args and "update-ref" in c.args[0]]
    assert len(update_ref_calls) == 1
    assert update_ref_calls[0].args[0] == [
        "git",
        "update-ref",
        "refs/harness-recovery/my-branch-deadbeef1234",
        "deadbeef1234",
    ]
    # the local branch must still get force-checked-out-away-from and deleted afterward
    checkout_calls = [c for c in mock_run.call_args_list if c.args and "checkout" in c.args[0]]
    branch_delete_calls = [c for c in mock_run.call_args_list if c.args and c.args[0][:3] == ["git", "branch", "-D"]]
    assert checkout_calls and branch_delete_calls
    assert any("preserved it at" in r.message for r in caplog.records)


async def test_git_restore_skips_recovery_ref_when_branch_fully_pushed(tmp_path, caplog):
    mock_run = AsyncMock(side_effect=_git_restore_side_effect(is_ancestor_returncode=0))
    with patch("dotharness.pr_review.runners.common.run_cmd", mock_run), caplog.at_level("WARNING"):
        await git_restore("orig-sha", "my-branch", str(tmp_path), {})
    update_ref_calls = [c for c in mock_run.call_args_list if c.args and "update-ref" in c.args[0]]
    assert update_ref_calls == []
    assert not any("preserved it at" in r.message for r in caplog.records)


async def test_git_restore_skips_preserve_check_when_branch_empty(tmp_path):
    mock_run = AsyncMock(return_value=MagicMock(returncode=0, stdout=b"", stderr=b""))
    with patch("dotharness.pr_review.runners.common.run_cmd", mock_run):
        await git_restore("orig-sha", "", str(tmp_path), {})
    # no branch name means nothing to preserve or delete: only the rebase detection and the checkout happen
    cmds = [c.args[0] for c in mock_run.call_args_list]
    assert [c for c in cmds if c[:2] != ["git", "rev-parse"]] == [cmds[-1]]
    assert cmds[-1][:2] == ["git", "checkout"]


async def test_git_restore_logs_error_when_recovery_ref_cannot_be_saved(tmp_path, caplog):
    def side_effect(cmd, **kwargs):
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"deadbeef1234\n", stderr=b"")
        if "merge-base" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        if "update-ref" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"failed")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("dotharness.pr_review.runners.common.run_cmd", side_effect=side_effect), caplog.at_level("ERROR"):
        await git_restore("orig-sha", "my-branch", str(tmp_path), {})
    assert any("could NOT be preserved" in r.message for r in caplog.records)


async def test_git_restore_completes_when_cancelled(tmp_path):
    import asyncio

    started = asyncio.Event()
    calls = []

    async def slow_run(cmd, **kwargs):
        calls.append(cmd)
        started.set()
        await asyncio.sleep(0.05)
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("dotharness.pr_review.runners.common.run_cmd", slow_run):
        task = asyncio.ensure_future(git_restore("orig-sha", "", str(tmp_path), {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("checkout" in c for c in calls)


def _rebase_side_effect(*, in_progress, abort_returncode=0):
    """run_cmd fake: ``--git-path`` answers point at a real marker dir only when ``in_progress``."""

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--git-path"]:
            return MagicMock(returncode=0, stdout=f"{cmd[3]}\n".encode(), stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"deadbeef1234\n", stderr=b"")
        if cmd[:3] == ["git", "rebase", "--abort"]:
            return MagicMock(returncode=abort_returncode, stdout=b"", stderr=b"abort failed")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    return side_effect


async def test_git_restore_aborts_a_rebase_in_progress_before_the_force_checkout(tmp_path):
    (tmp_path / "rebase-merge").mkdir()
    mock_run = AsyncMock(side_effect=_rebase_side_effect(in_progress=True))
    with patch("dotharness.pr_review.runners.common.run_cmd", mock_run):
        await git_restore("orig-sha", "", str(tmp_path), {})
    cmds = [c.args[0] for c in mock_run.call_args_list]
    abort = cmds.index(["git", "rebase", "--abort"])
    checkout = next(i for i, c in enumerate(cmds) if c[:2] == ["git", "checkout"])
    assert abort < checkout
    assert all(c.kwargs["timeout"] == 60 for c in mock_run.call_args_list)


async def test_git_restore_does_not_abort_when_no_rebase_is_in_progress(tmp_path):
    mock_run = AsyncMock(side_effect=_rebase_side_effect(in_progress=False))
    with patch("dotharness.pr_review.runners.common.run_cmd", mock_run):
        await git_restore("orig-sha", "", str(tmp_path), {})
    cmds = [c.args[0] for c in mock_run.call_args_list]
    assert ["git", "rebase", "--abort"] not in cmds
    assert sum(c[:3] == ["git", "rev-parse", "--git-path"] for c in cmds) == 2


async def test_git_restore_still_force_checks_out_when_the_abort_fails(tmp_path, caplog):
    (tmp_path / "rebase-apply").mkdir()
    mock_run = AsyncMock(side_effect=_rebase_side_effect(in_progress=True, abort_returncode=1))
    with patch("dotharness.pr_review.runners.common.run_cmd", mock_run), caplog.at_level("WARNING"):
        await git_restore("orig-sha", "my-branch", str(tmp_path), {})
    cmds = [c.args[0] for c in mock_run.call_args_list]
    assert ["git", "checkout", "--recurse-submodules", "-f", "orig-sha"] in cmds
    assert ["git", "branch", "-D", "my-branch"] in cmds
    assert any("rebase --abort failed" in r.message for r in caplog.records)


async def test_git_restore_still_force_checks_out_when_rebase_detection_raises(tmp_path, caplog):
    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--git-path"]:
            raise OSError("boom")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    mock_run = AsyncMock(side_effect=side_effect)
    with patch("dotharness.pr_review.runners.common.run_cmd", mock_run), caplog.at_level("WARNING"):
        await git_restore("orig-sha", "", str(tmp_path), {})
    assert ["git", "checkout", "--recurse-submodules", "-f", "orig-sha"] in [c.args[0] for c in mock_run.call_args_list]
    assert any("could not check for or abort a rebase" in r.message for r in caplog.records)


def _git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)  # noqa: S607


def _conflicted_rebase_repo(tmp_path):
    """A repo whose ``feature`` branch is mid-rebase, stopped on a conflict that also added a new file."""
    repo = str(tmp_path)
    _git(repo, "init", "-b", "main")
    (tmp_path / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    original = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-b", "feature")
    (tmp_path / "f.txt").write_text("feature\n")
    (tmp_path / "added_on_feature.txt").write_text("new\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature")
    _git(repo, "checkout", "main")
    (tmp_path / "f.txt").write_text("main\n")
    (tmp_path / "added_on_main.txt").write_text("new\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main change")
    _git(repo, "checkout", "feature")
    assert _git(repo, "rebase", "main").returncode != 0
    return repo, original


def _marker_exists(repo, name):
    path = _git(repo, "rev-parse", "--git-path", name).stdout.strip()
    return os.path.exists(os.path.join(repo, path))


@pytest.mark.parametrize("cancel", [False, True])
async def test_git_restore_clears_a_real_stopped_rebase_and_leaves_a_clean_tree(tmp_path, cancel):
    import asyncio

    repo, original = _conflicted_rebase_repo(tmp_path)
    assert _marker_exists(repo, "rebase-merge") or _marker_exists(repo, "rebase-apply")
    task = asyncio.ensure_future(git_restore(original, "feature", repo, os.environ.copy()))
    if cancel:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert not _marker_exists(repo, "rebase-merge")
    assert not _marker_exists(repo, "rebase-apply")
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == original


_MULTI = RepositoryRef("team/proj/repo")
_AZURE_ORIGIN = "https://example.invalid/team/proj/_git/repo"


def _non_github_host():
    host = InMemoryCodeHost(repository="team/proj/repo")
    host.add_origin(_MULTI, _AZURE_ORIGIN)
    return host.code_host()


def _identity_config(working_dir):
    config = MagicMock()
    config.repo.name = "team/proj/repo"
    config.repo.working_dir = working_dir
    return config


def _clone_with_origin(path, url):
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S607
    if url is not None:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)  # noqa: S607
    return path


async def test_repo_identity_check_passes_on_non_github_host_for_correct_clone(tmp_path):
    clone = _clone_with_origin(tmp_path / "c", _AZURE_ORIGIN)
    check = repo_identity_check(_identity_config(clone), _non_github_host(), _MULTI)
    await check(clone)


async def test_repo_identity_check_rejects_another_repository_on_the_same_host(tmp_path):
    clone = _clone_with_origin(tmp_path / "c", "https://example.invalid/team/proj/_git/other")
    check = repo_identity_check(_identity_config(clone), _non_github_host(), _MULTI)
    with pytest.raises(RepoIdentityError, match="does not match the expected repo 'team/proj/repo'"):
        await check(clone)


async def test_repo_identity_check_rejects_missing_origin_and_non_toplevel(tmp_path):
    no_origin = _clone_with_origin(tmp_path / "n", None)
    check = repo_identity_check(_identity_config(no_origin), _non_github_host(), _MULTI)
    with pytest.raises(RepoIdentityError, match="no 'origin' remote"):
        await check(no_origin)
    clone = _clone_with_origin(tmp_path / "c", _AZURE_ORIGIN)
    (clone / "sub").mkdir()
    with pytest.raises(RepoIdentityError, match="not itself the toplevel"):
        await check(clone / "sub")


async def test_repo_identity_check_fails_closed_when_the_host_matcher_raises(tmp_path):
    clone = _clone_with_origin(tmp_path / "c", _AZURE_ORIGIN)
    host = MagicMock()
    host.repository.origin_matches = AsyncMock(side_effect=RuntimeError("boom https://user:tok@example.invalid"))
    check = repo_identity_check(_identity_config(clone), host, _MULTI)
    with pytest.raises(RepoIdentityError) as exc:
        await check(clone)
    assert "tok" not in str(exc.value)
    host.repository.origin_matches.assert_awaited_once_with(_MULTI, _AZURE_ORIGIN)


COMMON = "dotharness.pr_review.runners.common"


def _local_config(working_dir, *, provided=True):
    config = _identity_config(working_dir)
    config.repo.name_provided = provided
    return config


async def test_local_identity_check_is_none_and_builds_no_host_when_name_not_provided(tmp_path):
    with patch(f"{COMMON}.create_code_host") as create, patch(f"{COMMON}.host_selection"):
        assert await local_identity_check(_local_config(tmp_path, provided=False)) is None
    create.assert_not_called()


async def test_local_identity_check_uses_the_host_matcher(tmp_path):
    clone = _clone_with_origin(tmp_path / "c", _AZURE_ORIGIN)
    other = _clone_with_origin(tmp_path / "o", "https://example.invalid/team/proj/_git/other")
    with patch(f"{COMMON}.create_code_host", return_value=_non_github_host()), patch(f"{COMMON}.host_selection"):
        check = await local_identity_check(_local_config(clone))
    assert check is not None
    await check(clone)
    with pytest.raises(RepoIdentityError):
        await check(other)


@pytest.mark.parametrize("where", ["create", "resolve"])
async def test_local_identity_check_configuration_error_exits_1(tmp_path, capsys, where):
    host = MagicMock()
    host.repository.resolve = AsyncMock(side_effect=ConfigurationError("bad setting") if where == "resolve" else None)
    create = (
        patch(f"{COMMON}.create_code_host", side_effect=ConfigurationError("bad setting"))
        if where == "create"
        else patch(f"{COMMON}.create_code_host", return_value=host)
    )
    with create, patch(f"{COMMON}.host_selection"), pytest.raises(SystemExit) as exc:
        await local_identity_check(_local_config(tmp_path))
    assert exc.value.code == 1
    assert "bad setting" in capsys.readouterr().err


def _order_recorder(seen, dirty):
    async def probe(cwd, env):
        seen.append("probe")
        return dirty

    async def clean(*args, **kwargs):
        seen.append(("clean", args, kwargs))
        return True

    async def run(cmd, **kwargs):
        seen.append(cmd[1])
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    return probe, clean, run


@pytest.mark.parametrize(("dirty", "cleaned"), [(True, True), (False, False), (None, False)])
async def test_git_fetch_and_checkout_probes_before_checkout(tmp_path, caplog, dirty, cleaned):
    seen = []
    probe, clean, run = _order_recorder(seen, dirty)
    with (
        patch(f"{COMMON}.git_is_dirty", probe),
        patch(f"{COMMON}.git_clean_working_dir", clean),
        patch(f"{COMMON}.run_cmd", run),
        caplog.at_level("WARNING"),
    ):
        await git_fetch_and_checkout("feature-branch", str(tmp_path), {})
    clean_calls = [c for c in seen if isinstance(c, tuple)]
    assert (len(clean_calls) == 1) is cleaned
    if cleaned:
        assert clean_calls[0][1:] == ((str(tmp_path), {}), {})  # no sha: clean only
    assert seen.index("probe") < seen.index("checkout")
    if cleaned:
        assert seen.index(clean_calls[0]) < seen.index("checkout")
    assert ("could not probe" in caplog.text) is (dirty is None)


async def test_git_clean_if_dirty_never_calls_reset(tmp_path):
    seen = []
    probe, clean, run = _order_recorder(seen, True)
    with (
        patch(f"{COMMON}.git_is_dirty", probe),
        patch(f"{COMMON}.git_clean_working_dir", clean),
        patch(f"{COMMON}.run_cmd", run),
    ):
        await git_fetch_and_checkout("b", str(tmp_path), {})
    assert "reset" not in seen
