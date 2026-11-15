"""Shared async repo-identity and harness-repo guards (``dotharness.repo_guard``).

These cover what the pr-review sync ``repo_guard`` module used to test; that module was replaced by this one.
"""

import subprocess
from pathlib import Path

import pytest

from dotharness import repo_guard
from dotharness.code_host import reference
from dotharness.code_host.reference import identity_check
from dotharness.repo_guard import (
    RepoIdentityError,
    assert_harness_unchanged,
    assert_repo_identity,
    snapshot_harness_repo,
)
from dotharness.subprocess_runner import RunResult


@pytest.fixture(autouse=True)
def _clear_ssh_host_cache():
    """reference `_resolve_ssh_host` memoizes per process; keep a mocked `ssh -G` result from leaking across tests."""
    reference._ssh_hosts.clear()
    yield
    reference._ssh_hosts.clear()


def _init_repo(path, origin_url):
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S607
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin_url],  # noqa: S607
        check=True,
    )


def _commit(path, content="content"):
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)  # noqa: S607
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)  # noqa: S607
    (path / "file.txt").write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)  # noqa: S607
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", f"commit {content}"],  # noqa: S607
        check=True,
    )


async def test_passes_when_toplevel_and_origin_match(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    await identity_check("acme/frontend")(tmp_path)


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
async def test_matches_across_url_forms(tmp_path, origin_url):
    _init_repo(tmp_path, origin_url)
    await identity_check("acme/frontend")(tmp_path)


async def test_matches_via_ssh_config_host_alias(tmp_path, monkeypatch):
    real_run = reference.run

    async def fake_run(argv, **kwargs):
        if argv[:2] == ["ssh", "-G"]:
            assert argv[-1] == "github-personal"
            return RunResult(0, "hostname github.com\n", "", False)
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(reference, "run", fake_run)

    _init_repo(tmp_path, "git@github-personal:acme/frontend.git")
    await identity_check("acme/frontend")(tmp_path)


async def test_unresolvable_ssh_alias_fails_closed(tmp_path, monkeypatch):
    real_run = reference.run

    async def fake_run(argv, **kwargs):
        if argv[:2] == ["ssh", "-G"]:
            return RunResult(255, "", "ssh: not found", False)
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(reference, "run", fake_run)

    _init_repo(tmp_path, "git@some-alias:acme/frontend.git")
    with pytest.raises(RepoIdentityError, match="does not match"):
        await identity_check("acme/frontend")(tmp_path)


async def test_matcher_decides_and_repo_guard_names_no_host(tmp_path):
    _init_repo(tmp_path, "whatever://example/x")
    seen: list[str] = []

    async def accept(url: str) -> bool:
        seen.append(url)
        return True

    async def reject(url: str) -> bool:
        return False

    await assert_repo_identity(tmp_path, accept)
    assert seen == ["whatever://example/x"]
    with pytest.raises(RepoIdentityError, match="does not match"):
        await assert_repo_identity(tmp_path, reject, "some/repo")


def test_repo_guard_source_names_no_host():
    source = Path(repo_guard.__file__).read_text()
    assert "github" not in source.lower()
    assert "ssh" not in source.lower()


async def test_non_github_host_and_option_like_host_do_not_match(tmp_path):
    for url in ("https://gitlab.com/acme/frontend.git", "ssh://git@-oProxyCommand=x/acme/frontend"):
        fresh = tmp_path / url.split("/")[2].replace("@", "_").replace("=", "_")
        fresh.mkdir()
        _init_repo(fresh, url)
        with pytest.raises(RepoIdentityError, match="does not match"):
            await identity_check("acme/frontend")(fresh)


async def test_raises_on_mismatched_origin(tmp_path):
    _init_repo(tmp_path, "git@github.com:alexeieleusis/dotharness.git")
    with pytest.raises(RepoIdentityError, match="does not match"):
        await identity_check("acme/frontend")(tmp_path)


async def test_raises_when_working_dir_is_not_a_git_repo(tmp_path):
    with pytest.raises(RepoIdentityError, match="not inside a git repository"):
        await identity_check("acme/frontend")(tmp_path)


async def test_raises_when_working_dir_is_a_subdirectory_of_a_repo(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/frontend.git")
    subdir = tmp_path / "packages" / "web"
    subdir.mkdir(parents=True)
    with pytest.raises(RepoIdentityError, match="not itself the toplevel"):
        await identity_check("acme/frontend")(subdir)


async def test_raises_when_origin_remote_is_missing(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S607
    with pytest.raises(RepoIdentityError, match="no 'origin' remote"):
        await identity_check("acme/frontend")(tmp_path)


async def test_snapshot_uses_explicit_root_and_detects_head_move(tmp_path):
    harness = tmp_path / "harness"
    other = tmp_path / "other"
    harness.mkdir()
    other.mkdir()
    _init_repo(harness, "git@github.com:acme/harness.git")
    _commit(harness, "one")
    snapshot = await snapshot_harness_repo(other, harness)
    assert snapshot is not None
    assert snapshot[0] == harness.resolve()
    await assert_harness_unchanged(snapshot)
    _commit(harness, "two")
    with pytest.raises(RepoIdentityError, match="HEAD moved"):
        await assert_harness_unchanged(snapshot)


async def test_snapshot_is_none_when_cwd_is_the_harness_repo(tmp_path):
    _init_repo(tmp_path, "git@github.com:acme/harness.git")
    _commit(tmp_path)
    assert await snapshot_harness_repo(tmp_path, tmp_path) is None


async def test_snapshot_raises_when_harness_head_unreadable(tmp_path):
    harness = tmp_path / "harness"
    other = tmp_path / "other"
    harness.mkdir()
    other.mkdir()
    _init_repo(harness, "git@github.com:acme/harness.git")
    with pytest.raises(RepoIdentityError, match="could not read HEAD"):
        await snapshot_harness_repo(other, harness)


async def test_assert_harness_unchanged_accepts_none():
    await assert_harness_unchanged(None)


async def test_ssh_alias_failure_is_not_cached(monkeypatch):
    calls = []

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return RunResult(stdout="", stderr="", returncode=255, timed_out=True)
        return RunResult(stdout="user git\nhostname github.com\n", stderr="", returncode=0)

    monkeypatch.setattr(reference, "run", fake_run)
    url = "git@work-gh:org/repo.git"
    assert await reference.origin_repository(url) is None
    assert await reference.origin_repository(url) == reference.RepositoryRef("org/repo")
    assert len(calls) == 2


async def test_origin_repository_strips_port():
    assert await reference.origin_repository("ssh://git@github.com:22/o/r") == reference.RepositoryRef("o/r")
