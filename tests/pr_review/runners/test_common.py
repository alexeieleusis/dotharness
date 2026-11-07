import os
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.pr_review.runners.common import (
    FatalGitError,
    add_reviewer,
    get_current_user,
    get_gh_token,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    run_cmd,
)
from dotharness.subprocess_runner import build_subprocess_env


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


async def test_get_gh_token_strips_whitespace():
    assert (await get_gh_token("printf '  tok123\\n  '")) == "tok123"


async def test_get_gh_token_nonzero_exit_raises_even_with_partial_stdout():
    # A nonzero exit means gh_token_cmd itself failed — trusting stray stdout here
    # (e.g. a warning banner some wrapper printed before failing) would silently hand
    # back a bogus "token" instead of surfacing the failure.
    with pytest.raises(RuntimeError, match="auth error") as exc:
        await get_gh_token("printf partial-token; printf 'auth error' >&2; exit 1")
    assert "exit 1" in str(exc.value)


async def test_get_gh_token_nonzero_exit_empty_stdout_raises():
    with pytest.raises(RuntimeError, match="auth failed"):
        await get_gh_token("printf 'auth failed' >&2; exit 1")


async def test_get_gh_token_error_does_not_leak_command():
    # gh_token_cmd is user config and may embed a literal credential (decision D-9).
    with pytest.raises(RuntimeError) as exc:
        await get_gh_token("echo SECRET-LITERAL >/dev/null; exit 3")
    assert "SECRET-LITERAL" not in str(exc.value)


async def test_get_gh_token_timeout_raises_without_command_and_without_hanging():
    # A backgrounded grandchild keeps the pipe open; the shared runner's process-group kill must still return.
    with (
        patch("dotharness.pr_review.runners.common.TIMEOUT_GH", 1),
        pytest.raises(RuntimeError, match="timed out") as exc,
    ):
        await get_gh_token("sleep 30 & echo SECRET-LITERAL; sleep 30")
    assert "SECRET-LITERAL" not in str(exc.value)


async def test_get_gh_token_stderr_only_raises():
    # Exit 0 but blank stdout: build_subprocess_env would treat this token as falsy
    # and silently skip setting GITHUB_TOKEN, so this must raise rather than return ""
    # — an unset GITHUB_TOKEN makes every subsequent `gh` call fall back to whichever
    # account `gh auth status` currently has active, not the one gh_token_cmd names.
    with pytest.raises(RuntimeError, match="token not configured"):
        await get_gh_token("printf 'token not configured' >&2")


async def test_add_reviewer_logs_error_on_failure(caplog):
    # preserve_reviewer_request calls add_reviewer from a `finally` with nothing else
    # watching the outcome — a failed re-add must be logged, or it's invisible.
    with (
        patch(
            "dotharness.pr_review.runners.common.run_cmd",
            return_value=MagicMock(returncode=1, stderr=b"could not add reviewer: not found"),
        ),
        caplog.at_level("ERROR"),
    ):
        await add_reviewer(9, "acme/frontend", "alice", {})
    assert any("could not add reviewer" in r.message for r in caplog.records)


async def test_add_reviewer_silent_on_success(caplog):
    with (
        patch("dotharness.pr_review.runners.common.run_cmd", return_value=MagicMock(returncode=0, stderr=b"")),
        caplog.at_level("ERROR"),
    ):
        await add_reviewer(9, "acme/frontend", "alice", {})
    assert not caplog.records


async def test_get_current_user_returns_login_on_success():
    with patch(
        "dotharness.pr_review.runners.common.run_cmd",
        return_value=MagicMock(returncode=0, stdout=b"alice\n", stderr=b""),
    ):
        assert (await get_current_user({})) == "alice"


async def test_get_current_user_logs_and_returns_blank_on_failure(caplog):
    # Fails open (returns "" rather than raising) since every caller already treats a
    # blank current_user as a safe no-op — but the failure must still be logged.
    with (
        patch(
            "dotharness.pr_review.runners.common.run_cmd",
            return_value=MagicMock(returncode=1, stdout=b"", stderr=b"gh: token revoked"),
        ),
        caplog.at_level("ERROR"),
    ):
        assert (await get_current_user({})) == ""
    assert any("token revoked" in r.message for r in caplog.records)


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
