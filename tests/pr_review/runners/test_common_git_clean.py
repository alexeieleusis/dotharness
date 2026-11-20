import asyncio
import logging
import os
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from dotharness.pr_review.runners.common import TIMEOUT_GIT, git_clean_working_dir, git_is_dirty

RUN_CMD = "dotharness.pr_review.runners.common.run_cmd"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S607


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text("cache/\n")
    (root / "a.txt").write_text("a")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "new.txt").write_text("tracked on feature")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "two")
    _git(root, "checkout", "-q", "--detach", "main")
    return root


def _env():
    return dict(os.environ)


async def test_clean_removes_colliding_untracked_file_keeps_ignored(repo, tmp_path, caplog):
    (repo / "new.txt").write_text("debris")
    (repo / "debris_dir").mkdir()
    (repo / "debris_dir" / "x").write_text("x")
    (repo / "cache").mkdir()
    (repo / "cache" / "keep").write_text("keep")
    outside = tmp_path / "outside.txt"
    outside.write_text("safe")
    failing = subprocess.run(["git", "checkout", "-B", "feature", "feature"], cwd=repo, capture_output=True)  # noqa: S607
    assert failing.returncode != 0
    with caplog.at_level(logging.WARNING):
        assert await git_clean_working_dir(str(repo), _env()) is True
    assert not (repo / "new.txt").exists()
    assert not (repo / "debris_dir").exists()
    assert (repo / "cache" / "keep").exists()
    assert outside.read_text() == "safe"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "new.txt" in warnings[0].getMessage() and "debris_dir/" in warnings[0].getMessage()
    _git(repo, "checkout", "-q", "-B", "feature", "feature")


async def test_clean_with_sha_checks_out_first_and_clean_tree_is_silent(repo, caplog):
    sha = subprocess.run(["git", "rev-parse", "main"], cwd=repo, capture_output=True, text=True).stdout.strip()  # noqa: S607
    with caplog.at_level(logging.WARNING):
        assert await git_clean_working_dir(str(repo), _env(), sha=sha) is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_timeout_leaves_no_debris_after_clean(repo):
    (repo / "debris").write_text("x")
    with patch(RUN_CMD, AsyncMock(side_effect=subprocess.TimeoutExpired("git", 60))):
        assert await git_clean_working_dir(str(repo), _env(), sha="abc") is False
    assert (repo / "debris").exists()
    assert await git_clean_working_dir(str(repo), _env()) is True
    assert not (repo / "debris").exists()


async def test_argv_cwd_timeout_and_no_path_or_x(tmp_path):
    ok = subprocess.CompletedProcess([], 0, b"Would remove a\n", b"")
    mock = AsyncMock(return_value=ok)
    with patch(RUN_CMD, mock):
        assert await git_clean_working_dir(str(tmp_path), {"K": "v"}, sha="s1") is True
    argvs = [c.args[0] for c in mock.call_args_list]
    assert argvs == [
        ["git", "checkout", "--recurse-submodules", "-f", "s1"],
        ["git", "clean", "-fdn"],
        ["git", "clean", "-fd"],
    ]
    for c in mock.call_args_list:
        assert c.kwargs["cwd"] == str(tmp_path)
        assert c.kwargs["timeout"] == TIMEOUT_GIT
        assert c.kwargs["check"] is False
        assert not any(a in ("-x", "-X", "-ff") for a in c.args[0])


async def test_without_sha_skips_checkout(tmp_path):
    mock = AsyncMock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    with patch(RUN_CMD, mock):
        await git_clean_working_dir(str(tmp_path), {})
    assert [c.args[0] for c in mock.call_args_list] == [["git", "clean", "-fdn"], ["git", "clean", "-fd"]]


@pytest.mark.parametrize("exc", [OSError("boom"), FileNotFoundError("git")])
async def test_never_raises_on_os_errors(tmp_path, exc):
    with patch(RUN_CMD, AsyncMock(side_effect=exc)):
        assert await git_clean_working_dir(str(tmp_path), {}, sha="s") is False


async def test_nonzero_real_clean_returns_false(tmp_path):
    def side_effect(cmd, **_):
        rc = 1 if cmd == ["git", "clean", "-fd"] else 0
        return subprocess.CompletedProcess(cmd, rc, b"", b"fatal: nope\nmore")

    with patch(RUN_CMD, AsyncMock(side_effect=side_effect)):
        assert await git_clean_working_dir(str(tmp_path), {}) is False


async def test_failed_restore_checkout_returns_false_even_if_clean_succeeds(tmp_path):
    def side_effect(cmd, **_):
        rc = 1 if cmd[:2] == ["git", "checkout"] else 0
        return subprocess.CompletedProcess(cmd, rc, b"", b"fatal: Unable to create index.lock")

    mock = AsyncMock(side_effect=side_effect)
    with patch(RUN_CMD, mock):
        assert await git_clean_working_dir(str(tmp_path), {}, sha="s") is False
    assert ["git", "clean", "-fd"] in [c.args[0] for c in mock.call_args_list]


async def test_clean_completes_when_cancelled(tmp_path):
    started = asyncio.Event()
    finished = []

    async def slow(cmd, **_):
        started.set()
        await asyncio.sleep(0.05)
        finished.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with patch(RUN_CMD, side_effect=slow):
        task = asyncio.ensure_future(git_clean_working_dir(str(tmp_path), {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert ["git", "clean", "-fd"] in finished


async def test_is_dirty_true_false_none(tmp_path):
    def result(rc, out=b"", err=b""):
        return AsyncMock(return_value=subprocess.CompletedProcess([], rc, out, err))

    with patch(RUN_CMD, result(0, b" M a.txt\n")):
        assert await git_is_dirty(str(tmp_path), {}) is True
    with patch(RUN_CMD, result(0, b"")):
        assert await git_is_dirty(str(tmp_path), {}) is False
    with patch(RUN_CMD, result(128, b"", b"fatal")):
        assert await git_is_dirty(str(tmp_path), {}) is None
    with patch(RUN_CMD, AsyncMock(side_effect=subprocess.TimeoutExpired("git", 60))):
        assert await git_is_dirty(str(tmp_path), {}) is None
    with patch(RUN_CMD, AsyncMock(side_effect=OSError("x"))):
        assert await git_is_dirty(str(tmp_path), {}) is None


async def test_is_dirty_real_repo(repo):
    assert await git_is_dirty(str(repo), _env()) is False
    (repo / "u").write_text("x")
    assert await git_is_dirty(str(repo), _env()) is True
