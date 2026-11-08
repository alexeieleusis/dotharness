"""Tests for dotharness.pr_review.runners.local_review (real temporary git repos)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from dotharness.pr_review.runners import common
from dotharness.pr_review.runners.common import get_changed_files, get_file_diff, is_ancestor
from dotharness.pr_review.runners.local_review import (
    LocalReviewError,
    NothingToReviewError,
    check_preconditions,
)
from dotharness.pr_review.runners.local_review import git as lr_git
from dotharness.pr_review.runners.local_review import review as lr_review
from dotharness.pr_review.runners.local_review.git import (
    check_output_dir_outside_repo,
    check_templates,
    compute_merge_base,
    resolve_base_ref,
)


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def commit(repo: Path, name: str, content: str = "x") -> None:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"add {name}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = (tmp_path / "repo").resolve()
    r.mkdir()
    git(r, "init", "-b", "main")
    commit(r, "a.txt")
    git(r, "checkout", "-b", "feature")
    commit(r, "b.txt")
    return r


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = subprocess.Popen

    def spy(args, *a, **kw):
        calls.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(common.subprocess, "Popen", spy)
    return calls


# ---- base ref resolution ----


async def test_explicit_base_wins(repo: Path) -> None:
    git(repo, "branch", "other", "main")
    assert (await resolve_base_ref(repo, "other", "main")) == "other"


async def test_config_base_used_when_no_explicit(repo: Path) -> None:
    git(repo, "branch", "other", "main")
    assert (await resolve_base_ref(repo, None, "other")) == "other"


async def test_explicit_base_missing_does_not_fall_through(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="nope"):
        await resolve_base_ref(repo, "nope", "main")


async def test_config_base_missing_does_not_fall_through(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="nope"):
        await resolve_base_ref(repo, None, "nope")


async def test_origin_head_used_when_present(repo: Path) -> None:
    git(repo, "update-ref", "refs/remotes/origin/trunk", "main")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert (await resolve_base_ref(repo)) == "origin/trunk"


async def test_dangling_origin_head_falls_through_to_main(repo: Path) -> None:
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
    assert (await resolve_base_ref(repo)) == "main"


async def test_falls_back_to_main(repo: Path) -> None:
    assert (await resolve_base_ref(repo)) == "main"


async def test_nothing_resolves_lists_tried(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-b", "dev")
    commit(r, "a")
    with pytest.raises(LocalReviewError, match=r"tried:.*origin/HEAD.*main"):
        await resolve_base_ref(r)


async def test_compute_merge_base(repo: Path) -> None:
    assert (await compute_merge_base(repo, "main")) == git(repo, "rev-parse", "main")


async def test_compute_merge_base_unrelated(repo: Path) -> None:
    git(repo, "checkout", "--orphan", "orph")
    commit(repo, "o.txt")
    with pytest.raises(LocalReviewError, match="no merge base"):
        await compute_merge_base(repo, "main")


# ---- preconditions ----


async def test_happy_path(repo: Path) -> None:
    t = await check_preconditions(repo)
    assert t.branch == "feature"
    assert t.base_ref == "main"
    assert t.base_sha == git(repo, "rev-parse", "main")
    assert t.head_sha == git(repo, "rev-parse", "HEAD")


async def test_not_toplevel(repo: Path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    with pytest.raises(LocalReviewError, match="toplevel"):
        await check_preconditions(sub)


async def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(LocalReviewError, match="not inside a git repository"):
        await check_preconditions(tmp_path)


async def test_detached_head(repo: Path) -> None:
    git(repo, "checkout", "--detach")
    with pytest.raises(LocalReviewError, match="check out a branch"):
        await check_preconditions(repo)


async def test_dirty_tracked_blocks(repo: Path) -> None:
    (repo / "a.txt").write_text("changed")
    with pytest.raises(LocalReviewError, match=r"a\.txt.*not reviewed"):
        await check_preconditions(repo)


async def test_dirty_names_only_first_five(repo: Path) -> None:
    for i in range(7):
        commit(repo, f"f{i}.txt")
        (repo / f"f{i}.txt").write_text("changed")
    with pytest.raises(LocalReviewError, match=r"and 2 more") as ei:
        await check_preconditions(repo)
    assert ei.value.args[0].count(".txt") == 5


async def test_untracked_does_not_block(repo: Path) -> None:
    (repo / "new.txt").write_text("u")
    assert (await check_preconditions(repo)).branch == "feature"


async def test_explicit_bad_base(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="nope"):
        await check_preconditions(repo, base="nope")


async def test_nothing_to_review_on_base_branch(repo: Path) -> None:
    git(repo, "checkout", "main")
    with pytest.raises(NothingToReviewError, match="Nothing to review: main has no commits beyond main"):
        await check_preconditions(repo)


async def test_nothing_to_review_no_new_commits(repo: Path) -> None:
    git(repo, "checkout", "-b", "same", "main")
    with pytest.raises(NothingToReviewError, match="same has no commits beyond main"):
        await check_preconditions(repo)


def test_nothing_to_review_is_not_an_error() -> None:
    assert not issubclass(NothingToReviewError, LocalReviewError)
    assert not issubclass(LocalReviewError, NothingToReviewError)


async def test_skip_review_allows_nothing_to_review(repo: Path) -> None:
    git(repo, "checkout", "main")
    t = await check_preconditions(repo, review_phase=False)
    assert t.base_sha == t.head_sha


# ---- output dir ----


async def test_output_dir_inside_repo_refused(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="inside the reviewed repo"):
        await check_preconditions(repo, output_root=repo / "out")


def test_output_dir_equal_repo_refused(repo: Path) -> None:
    with pytest.raises(LocalReviewError):
        check_output_dir_outside_repo(repo, repo)


def test_output_dir_symlink_into_repo_refused(repo: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(repo)
    with pytest.raises(LocalReviewError):
        check_output_dir_outside_repo(repo, link / "out")


def test_output_dir_tilde_resolved(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(repo))
    with pytest.raises(LocalReviewError):
        check_output_dir_outside_repo(repo, Path("~/reviews"))


async def test_output_dir_outside_ok(repo: Path, tmp_path: Path) -> None:
    check_output_dir_outside_repo(repo, tmp_path / "out")
    assert (await check_preconditions(repo, output_root=tmp_path / "out")).branch == "feature"
    assert not (tmp_path / "out").exists()


# ---- templates ----


def test_templates_present(tmp_path: Path) -> None:
    d = tmp_path / "pr-review"
    d.mkdir()
    (d / "a.md").write_text("x")
    check_templates(tmp_path, ["a.md"])


def test_template_missing_names_path(tmp_path: Path) -> None:
    (tmp_path / "pr-review").mkdir()
    with pytest.raises(LocalReviewError, match=r"local-address-finding\.md"):
        check_templates(tmp_path, ["local-address-finding.md"])


# ---- no network ----


async def test_no_network_git_commands(repo: Path, git_calls: list[list[str]]) -> None:
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
    git_calls.clear()
    await check_preconditions(repo)
    subcommands = {c[1] for c in git_calls}
    assert subcommands
    assert subcommands.isdisjoint({"fetch", "pull", "remote", "push", "clone", "ls-remote"})
    assert all(c[0] == "git" for c in git_calls)


# ---- diff hooks and is_ancestor ----


def _env() -> dict:
    # Ignore the developer's global git config (e.g. color.diff=always).
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


async def test_get_changed_files_rev_range_merge_base(repo: Path) -> None:
    base = await compute_merge_base(repo, "main")
    git(repo, "checkout", "main")
    commit(repo, "main-only.txt")
    git(repo, "checkout", "feature")
    commit(repo, "c.txt")
    files = await get_changed_files("main", str(repo), _env(), rev_range=f"{base}..HEAD")
    assert sorted(files) == ["b.txt", "c.txt"]


async def test_get_file_diff_rev_range_two_args(repo: Path) -> None:
    base = await compute_merge_base(repo, "main")
    diff = await get_file_diff("b.txt", "main", str(repo), _env(), rev_range=f"{base} HEAD")
    assert "+++ b/b.txt" in diff
    assert "+x" in diff
    assert (await get_file_diff("a.txt", "main", str(repo), _env(), rev_range=f"{base} HEAD")) == ""


async def test_default_range_still_origin_triple_dot(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    real = subprocess.Popen

    def spy(args, *a, **kw):
        seen.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(common.subprocess, "Popen", spy)
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    assert (await get_changed_files("main", str(repo), _env())) == ["b.txt"]
    assert "+x" in (await get_file_diff("b.txt", "main", str(repo), _env()))
    diffs = [c for c in seen if c[:2] == ["git", "diff"]]
    assert ["git", "diff", "--name-only", "origin/main...HEAD"] in diffs
    assert ["git", "diff", "origin/main...HEAD", "--", "b.txt"] in diffs


async def test_is_ancestor(repo: Path) -> None:
    base = await compute_merge_base(repo, "main")
    head = git(repo, "rev-parse", "HEAD")
    assert (await is_ancestor(base, head, str(repo), _env())) is True
    assert (await is_ancestor(head, base, str(repo), _env())) is False


async def test_git_timeout_becomes_failed_process(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(cmd: list[str], **_kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(lr_git, "run_cmd", boom)  # `git.py` owns the `run_cmd` lookup
    proc = await lr_git.git(repo, "status")
    assert proc.returncode == 124
    assert "timed out" in proc.stderr
    assert "git status failed" in (await lr_review._guard_violation(repo, "abc") or "")
