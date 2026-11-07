"""`review._read_head_sha` replaces the removed pr-review sync `repo_guard.head_sha`."""

import subprocess

import pytest

from dotharness.pr_review.runners.local_review.review import _read_head_sha
from dotharness.repo_guard import RepoIdentityError


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True)  # noqa: S607


def test_head_sha_returns_current_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-q", "-m", "c")
    expected = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert _read_head_sha(tmp_path) == expected


def test_head_sha_raises_when_no_commits_exist(tmp_path):
    _git(tmp_path, "init", "-q")
    with pytest.raises(RepoIdentityError, match="could not read HEAD"):
        _read_head_sha(tmp_path)
