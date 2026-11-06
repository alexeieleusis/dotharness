import subprocess
from pathlib import Path

import pytest

from dotharness.git_utils import GitError, git_toplevel


def test_toplevel_of_repo_root_and_subdirectory(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S607
    expected = repo.resolve()
    assert git_toplevel(repo).resolve() == expected
    assert git_toplevel(sub).resolve() == expected
    assert isinstance(git_toplevel(sub), Path)


def test_non_git_directory_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(GitError):
        git_toplevel(tmp_path)


def test_missing_git_binary_raises_git_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(GitError, match="could not run git"):
        git_toplevel(tmp_path)
