import subprocess

import pytest

from harness.repo_guard import RepoIdentityError, assert_repo_identity


def _init_repo(path, origin_url):
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        ["git", "-C", str(path), "remote", "add", "origin", origin_url],  # noqa: S607
        check=True,
    )


def test_passes_when_toplevel_and_origin_match(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    assert_repo_identity(tmp_path, "acme/frontend")


@pytest.mark.parametrize(
    "origin_url",
    [
        "git@github.com:acme/frontend.git",
        "git@github.com:acme/frontend",
        "https://github.com/acme/frontend.git",
        "https://github.com/acme/frontend",
        "ssh://git@github.com/acme/frontend.git",
    ],
)
def test_matches_across_url_forms(tmp_path, origin_url):
    _init_repo(tmp_path, origin_url)
    assert_repo_identity(tmp_path, "acme/frontend")


def test_raises_on_mismatched_origin(tmp_path):
    _init_repo(tmp_path, "git@github.com:alexeieleusis/dotharness.git")
    with pytest.raises(RepoIdentityError, match="does not match"):
        assert_repo_identity(tmp_path, "acme/frontend")


def test_raises_when_working_dir_is_not_a_git_repo(tmp_path):
    with pytest.raises(RepoIdentityError, match="not inside a git repository"):
        assert_repo_identity(tmp_path, "acme/frontend")


def test_raises_when_working_dir_is_a_subdirectory_of_a_repo(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    subdir = tmp_path / "packages" / "web"
    subdir.mkdir(parents=True)
    with pytest.raises(RepoIdentityError, match="not itself the toplevel"):
        assert_repo_identity(subdir, "acme/frontend")


def test_raises_when_origin_remote_is_missing(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    with pytest.raises(RepoIdentityError, match="no 'origin' remote"):
        assert_repo_identity(tmp_path, "acme/frontend")
