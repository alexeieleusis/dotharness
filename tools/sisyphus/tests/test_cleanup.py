"""Branch cleanup against real temporary git repositories with a local bare ``origin``.

No network, no harness, no dotharness repo. Every git command is logged so the tests can assert that
``-D`` is never used.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from sisyphus.cleanup import ABSENT, DELETED, KEPT, cleanup_branch
from sisyphus.orchestrator import CommandResult

BRANCH = "sisyphus/eng-1-abcd1234"
ITEM = "ENG-1"


def _run(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)  # noqa: S603, S607
    return res.stdout.strip()


class Repo:
    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        self.clone = tmp / "clone"
        self.other = tmp / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)  # noqa: S603, S607
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True, capture_output=True)  # noqa: S603, S607
        for repo in (self.clone,):
            _run(repo, "config", "user.email", "t@example.com")
            _run(repo, "config", "user.name", "t")
        self.commit(self.clone, "a.txt", "one")
        _run(self.clone, "push", "-q", "origin", "main")
        self.log: list[list[str]] = []

    def commit(self, repo: Path, name: str, text: str) -> None:
        (repo / name).write_text(text)
        _run(repo, "add", name)
        _run(repo, "commit", "-q", "-m", f"add {name}")

    def make_branch(self, *, push: bool = True) -> None:
        _run(self.clone, "checkout", "-q", "-b", BRANCH)
        self.commit(self.clone, "b.txt", "work")
        if push:
            _run(self.clone, "push", "-q", "origin", BRANCH)

    def advance_origin_main(self) -> None:
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)], check=True, capture_output=True)  # noqa: S603, S607
        _run(self.other, "config", "user.email", "t@example.com")
        _run(self.other, "config", "user.name", "t")
        self.commit(self.other, "c.txt", "more")
        _run(self.other, "push", "-q", "origin", "main")

    def git(self, args: Sequence[str]) -> CommandResult:
        self.log.append(list(args))
        res = subprocess.run(["git", "-C", str(self.clone), *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
        return CommandResult(res.returncode, res.stdout, res.stderr)

    def cleanup(self):
        return cleanup_branch(self.git, item=ITEM, branch=BRANCH, base="main")

    def branches(self) -> list[str]:
        return _run(self.clone, "branch", "--format=%(refname:short)").splitlines()

    def current(self) -> str:
        return _run(self.clone, "rev-parse", "--abbrev-ref", "HEAD")

    def assert_no_capital_d(self) -> None:
        assert not any("-D" in args or "--force" in args for args in self.log)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_pushed_branch_is_deleted_even_when_unmerged_and_clone_returns_to_base(repo: Repo) -> None:
    repo.make_branch()
    out = repo.cleanup()
    assert out.status == DELETED
    assert BRANCH not in repo.branches()
    assert repo.current() == "main"
    assert _run(repo.clone, "status", "--porcelain") == ""
    assert _run(repo.origin, "branch", "--list", BRANCH)  # the remote branch is untouched
    assert ["branch", "-d", BRANCH] in repo.log
    repo.assert_no_capital_d()


def test_base_is_fast_forwarded_from_origin(repo: Repo) -> None:
    repo.make_branch()
    repo.advance_origin_main()
    assert repo.cleanup().status == DELETED
    assert (repo.clone / "c.txt").exists()
    assert _run(repo.clone, "rev-parse", "main") == _run(repo.origin, "rev-parse", "main")


def test_not_pushed_is_kept(repo: Repo) -> None:
    repo.make_branch(push=False)
    out = repo.cleanup()
    assert out.status == KEPT
    assert out.message == f"Branch kept: {ITEM} {BRANCH} not pushed (no such branch on origin)"
    assert BRANCH in repo.branches()
    assert repo.current() == BRANCH
    repo.assert_no_capital_d()


def test_local_commits_not_on_origin_are_kept(repo: Repo) -> None:
    repo.make_branch()
    repo.commit(repo.clone, "late.txt", "unpushed")
    out = repo.cleanup()
    assert out.status == KEPT
    assert "has 1 commit(s) not on origin" in out.message
    assert BRANCH in repo.branches()
    assert repo.current() == BRANCH


def test_upstream_tracking_is_not_trusted(repo: Repo) -> None:
    repo.make_branch()
    _run(repo.clone, "branch", f"--set-upstream-to=origin/{BRANCH}", BRANCH)
    repo.commit(repo.clone, "late.txt", "unpushed")
    assert repo.cleanup().status == KEPT
    assert BRANCH in repo.branches()


def test_dirty_clone_is_kept_untouched(repo: Repo) -> None:
    repo.make_branch()
    (repo.clone / "scratch.txt").write_text("mine")
    out = repo.cleanup()
    assert out.status == KEPT
    assert "dirty" in out.message
    assert (repo.clone / "scratch.txt").read_text() == "mine"
    assert repo.current() == BRANCH
    assert not any(args[0] in ("stash", "reset", "clean", "checkout", "branch") for args in repo.log)


def test_absent_branch_is_a_silent_no_op(repo: Repo) -> None:
    out = repo.cleanup()
    assert out.status == ABSENT
    assert len(repo.log) == 1


def test_idempotent_second_call(repo: Repo) -> None:
    repo.make_branch()
    assert repo.cleanup().status == DELETED
    assert repo.cleanup().status == ABSENT
    assert repo.current() == "main"


def test_failed_fast_forward_keeps_the_branch(repo: Repo) -> None:
    repo.make_branch()
    _run(repo.clone, "checkout", "-q", "main")
    repo.commit(repo.clone, "local-only.txt", "diverged")
    repo.advance_origin_main()
    _run(repo.clone, "checkout", "-q", BRANCH)
    out = repo.cleanup()
    assert out.status == KEPT
    assert "cannot be fast-forwarded" in out.message
    assert BRANCH in repo.branches()
    assert repo.current() == "main"  # on base, clean, nothing reset
    assert _run(repo.clone, "status", "--porcelain") == ""
    repo.assert_no_capital_d()


def test_detached_head_on_the_remote_branch_is_fine(repo: Repo) -> None:
    repo.make_branch()
    _run(repo.clone, "checkout", "-q", "--detach", BRANCH)
    assert repo.cleanup().status == DELETED
    assert repo.current() == "main"


def test_detached_head_ahead_of_origin_is_kept(repo: Repo) -> None:
    repo.make_branch()
    _run(repo.clone, "checkout", "-q", "--detach", BRANCH)
    repo.commit(repo.clone, "orphan.txt", "would be lost")
    out = repo.cleanup()
    assert out.status == KEPT
    assert "detached HEAD" in out.message
    assert BRANCH in repo.branches()


def test_unreachable_origin_keeps_the_branch(repo: Repo) -> None:
    repo.make_branch()
    _run(repo.clone, "remote", "set-url", "origin", str(repo.origin) + "-missing")
    out = repo.cleanup()
    assert out.status == KEPT
    assert BRANCH in repo.branches()
