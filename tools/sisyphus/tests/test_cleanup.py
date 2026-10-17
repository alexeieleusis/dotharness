"""Branch cleanup against real temporary git repositories with a local bare ``origin``.

No network, no harness, no dotharness repo. Every git command is logged so the tests can assert that
``-D`` is never used.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from sisyphus.cleanup import ABSENT, DELETED, KEPT, PrInfo, cleanup_branch
from sisyphus.orchestrator import CommandResult, GitHubError

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

    def merge_on_origin(self, *, squash: bool, delete_remote_branch: bool = True) -> None:
        """Merge the pushed branch into origin/main from a second clone, as GitHub would, then drop the branch."""
        if not self.other.exists():
            subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)], check=True, capture_output=True)  # noqa: S603, S607
            _run(self.other, "config", "user.email", "t@example.com")
            _run(self.other, "config", "user.name", "t")
        _run(self.other, "fetch", "-q", "origin")
        _run(self.other, "checkout", "-q", "main")
        _run(self.other, "pull", "-q", "--ff-only", "origin", "main")
        if squash:
            _run(self.other, "merge", "--squash", f"origin/{BRANCH}")
            _run(self.other, "commit", "-q", "-m", "squashed")
        else:
            _run(self.other, "merge", "--no-ff", "-q", "-m", "merge pr", f"origin/{BRANCH}")
        _run(self.other, "push", "-q", "origin", "main")
        if delete_remote_branch:
            _run(self.other, "push", "-q", "origin", "--delete", BRANCH)

    def head(self) -> str:
        return _run(self.clone, "rev-parse", BRANCH)

    def cleanup(self, pr: object = None, pr_ref: str | None = None):
        self.pr_calls: list[str] = []

        def lookup(ref: str) -> PrInfo | None:
            self.pr_calls.append(ref)
            if isinstance(pr, Exception):
                raise pr
            return pr if isinstance(pr, PrInfo) else None

        return cleanup_branch(
            self.git,
            item=ITEM,
            branch=BRANCH,
            base="main",
            pr_lookup=lookup if pr != "none-client" else None,
            pr_ref=pr_ref,
        )

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
    assert out.message.startswith(
        f"Branch kept: {ITEM} {BRANCH} no deletion proof held: (1) not pushed (no such branch"
    )
    assert "(2) head is not an ancestor of origin/main" in out.message
    assert "(3) no PR found for " + BRANCH in out.message
    assert BRANCH in repo.branches()
    assert repo.current() == BRANCH
    repo.assert_no_capital_d()


def test_local_commits_not_on_origin_are_kept(repo: Repo) -> None:
    repo.make_branch()
    repo.commit(repo.clone, "late.txt", "unpushed")
    out = repo.cleanup()
    assert out.status == KEPT
    assert "(1) has 1 commit(s) not on origin" in out.message
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


# ---------------------------------------------------------------- the three proofs


def test_done_message_names_proof_one(repo: Repo) -> None:
    repo.make_branch()
    out = repo.cleanup()
    assert out.status == DELETED
    assert "(proof 1: head " in out.message
    assert f"is an ancestor of origin/{BRANCH}" in out.message
    assert repo.pr_calls == []  # cheapest first: gh is never asked


def test_merge_commit_merge_with_remote_branch_gone_is_proof_two(repo: Repo) -> None:
    repo.make_branch()
    repo.merge_on_origin(squash=False)
    out = repo.cleanup()
    assert out.status == DELETED
    assert "(proof 2: head " in out.message and "is an ancestor of origin/main" in out.message
    assert BRANCH not in repo.branches()  # no local branch left behind
    assert repo.current() == "main"
    assert ["branch", "-d", BRANCH] in repo.log
    assert not any(a[0] == "branch" and a[1].startswith("--set-upstream") for a in repo.log)
    assert repo.pr_calls == []
    repo.assert_no_capital_d()


def test_rebase_style_merge_is_proof_two(repo: Repo) -> None:
    repo.make_branch()
    # a rebase merge on GitHub replays commits; a fast-forward of main to the branch head is the same shape
    _run(repo.clone, "checkout", "-q", "main")
    _run(repo.clone, "merge", "-q", "--ff-only", BRANCH)
    _run(repo.clone, "push", "-q", "origin", "main")
    _run(repo.clone, "push", "-q", "origin", "--delete", BRANCH)
    _run(repo.clone, "checkout", "-q", BRANCH)
    out = repo.cleanup()
    assert out.status == DELETED
    assert "proof 2" in out.message
    assert BRANCH not in repo.branches()


def test_unmerged_branch_with_nothing_pushed_names_all_three_proofs(repo: Repo) -> None:
    repo.make_branch(push=False)
    out = repo.cleanup(None)
    assert out.status == KEPT
    assert "(1) " in out.message and "(2) " in out.message and "(3) no PR found" in out.message
    assert repo.pr_calls == [BRANCH]
    assert BRANCH in repo.branches()


def test_pr_number_from_the_run_record_is_what_gh_is_asked(repo: Repo) -> None:
    repo.make_branch(push=False)
    repo.cleanup(None, pr_ref="17")
    assert repo.pr_calls == ["17"]


@pytest.mark.parametrize(
    ("info", "needle"),
    [
        (PrInfo(5, "OPEN", "x"), "PR #5 is OPEN, not MERGED"),
        (PrInfo(5, "CLOSED", "x"), "PR #5 is CLOSED, not MERGED"),
        (GitHubError("gh pr view failed (exit 1): boom"), "gh lookup of " + BRANCH + " failed: gh pr view failed"),
        (None, "no PR found for " + BRANCH),
        ("none-client", "no GitHub client available"),
    ],
)
def test_proof_three_failures_keep_the_branch_and_name_the_proofs(repo: Repo, info: object, needle: str) -> None:
    repo.make_branch(push=False)
    if isinstance(info, PrInfo) and info.head_sha == "x":
        info = PrInfo(info.number, info.state, repo.head())
    out = repo.cleanup(info)
    assert out.status == KEPT
    assert out.message.startswith(f"Branch kept: {ITEM} {BRANCH} no deletion proof held: (1) ")
    assert needle in out.message
    assert BRANCH in repo.branches()
    repo.assert_no_capital_d()


def test_proof_three_with_a_mismatching_head_keeps_the_branch(repo: Repo) -> None:
    repo.make_branch(push=False)
    out = repo.cleanup(PrInfo(5, "MERGED", "0" * 40))
    assert out.status == KEPT
    assert "PR #5 is MERGED but its head 000000000000 differs from the local head" in out.message


def test_squash_merge_with_remote_branch_gone_is_proven_but_kept_because_d_refuses(repo: Repo) -> None:
    """The open question: a squash-merged head is in neither HEAD nor an upstream, so plain -d refuses."""
    repo.make_branch()
    repo.merge_on_origin(squash=True)
    out = repo.cleanup(PrInfo(9, "MERGED", repo.head()), pr_ref="9")
    assert out.status == KEPT
    assert "proof (3) holds (PR #9 is MERGED with head " in out.message
    assert "needs git branch -D" in out.message
    assert BRANCH in repo.branches()  # not deleted: -D is not used
    assert repo.current() == "main"  # the clone is on base, clean
    assert _run(repo.clone, "status", "--porcelain") == ""
    assert repo.pr_calls == ["9"]
    assert ["branch", "-d", BRANCH] in repo.log
    repo.assert_no_capital_d()
    assert _run(repo.clone, "for-each-ref", "refs/sisyphus", "--format=%(refname)") == ""  # no recovery ref yet


def test_squash_merge_leaves_no_helper_refs_or_branches_behind(repo: Repo) -> None:
    repo.make_branch()
    repo.merge_on_origin(squash=True)
    before = _run(repo.clone, "for-each-ref", "--format=%(refname)")
    repo.cleanup(PrInfo(9, "MERGED", repo.head()))
    after = _run(repo.clone, "for-each-ref", "--format=%(refname)")
    assert set(after.splitlines()) - set(before.splitlines()) <= {"refs/remotes/origin/main"}
    assert sorted(repo.branches()) == sorted(["main", BRANCH])


def test_proofs_are_cheapest_first_and_gh_not_called_when_git_proves_it(repo: Repo) -> None:
    repo.make_branch()
    repo.merge_on_origin(squash=False)
    repo.cleanup(PrInfo(1, "MERGED", repo.head()))
    assert repo.pr_calls == []
    order = [a[0] for a in repo.log]
    assert order.index("ls-remote") < order.index("merge-base")


def test_dirty_clone_never_asks_gh(repo: Repo) -> None:
    repo.make_branch(push=False)
    (repo.clone / "scratch.txt").write_text("mine")
    repo.cleanup(PrInfo(1, "MERGED", repo.head()))
    assert repo.pr_calls == []
