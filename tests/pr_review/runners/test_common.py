import os
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.gh import build_subprocess_env
from dotharness.pr_review.runners.common import (
    FatalGitError,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    run_cmd,
)


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
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
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


def test_build_env_prepends_path():
    original_path = os.environ.get("PATH", "")
    env = build_subprocess_env(["/java/bin", "/node/bin"], {}, "tok")
    assert env["PATH"].startswith("/java/bin:/node/bin:")
    assert original_path in env["PATH"]


def test_build_env_sets_github_token():
    env = build_subprocess_env([], {}, "mytoken")
    assert env["GITHUB_TOKEN"] == "mytoken"  # noqa: S105


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
    # no branch name means nothing to preserve or delete: only the checkout happens
    assert mock_run.call_count == 1
    assert mock_run.call_args_list[0].args[0][:2] == ["git", "checkout"]


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
