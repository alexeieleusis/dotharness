import subprocess

import pytest

from harness.repo_guard import (
    RepoIdentityError,
    assert_repo_identity,
    assert_repo_unchanged,
    discover_repo_root,
    head_sha,
)


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


def test_matches_via_ssh_config_host_alias(tmp_path, monkeypatch):
    from harness import repo_guard

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] == ["ssh", "-G"]:
            assert args[2] == "github-personal"
            return subprocess.CompletedProcess(args, 0, stdout="hostname github.com\n", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(repo_guard.subprocess, "run", fake_run)

    _init_repo(tmp_path, "git@github-personal:acme/frontend.git")
    assert_repo_identity(tmp_path, "acme/frontend")


def test_unresolvable_ssh_alias_fails_closed(tmp_path, monkeypatch):
    from harness import repo_guard

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] == ["ssh", "-G"]:
            raise FileNotFoundError
        return real_run(args, **kwargs)

    monkeypatch.setattr(repo_guard.subprocess, "run", fake_run)

    _init_repo(tmp_path, "git@some-alias:acme/frontend.git")
    with pytest.raises(RepoIdentityError, match="does not match"):
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


def _commit(path, content="content"):
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)  # noqa: S603, S607
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)  # noqa: S603, S607
    (path / "file.txt").write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        ["git", "-C", str(path), "commit", "-q", "-m", f"commit {content}"],  # noqa: S607
        check=True,
    )


def test_discover_repo_root_finds_toplevel_from_subdirectory(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    subdir = tmp_path / "packages" / "web"
    subdir.mkdir(parents=True)
    assert discover_repo_root(subdir) == tmp_path.resolve()


def test_discover_repo_root_returns_none_outside_a_repo(tmp_path):
    assert discover_repo_root(tmp_path) is None


def test_head_sha_returns_current_commit(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    _commit(tmp_path)
    sha = head_sha(tmp_path)
    expected = subprocess.run(  # noqa: S603
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert sha == expected


def test_head_sha_raises_when_no_commits_exist(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    with pytest.raises(RepoIdentityError, match="could not read HEAD"):
        head_sha(tmp_path)


def test_assert_repo_unchanged_passes_when_head_is_stable(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    _commit(tmp_path)
    assert_repo_unchanged(tmp_path, head_sha(tmp_path))


def test_assert_repo_unchanged_raises_when_head_moved(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    _commit(tmp_path)
    stale_head = head_sha(tmp_path)
    _commit(tmp_path, content="second commit")
    with pytest.raises(RepoIdentityError, match="HEAD moved"):
        assert_repo_unchanged(tmp_path, stale_head)
