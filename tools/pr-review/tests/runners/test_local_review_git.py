"""Tests for harness.runners.local_review (real temporary git repos)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harness.runners import common, local_review
from harness.runners.common import get_changed_files, get_file_diff, is_ancestor
from harness.runners.local_review import (
    LocalReviewError,
    NothingToReviewError,
    check_output_dir_outside_repo,
    check_preconditions,
    check_templates,
    compute_merge_base,
    resolve_base_ref,
)


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(  # noqa: S603
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
    real = subprocess.run

    def spy(args, *a, **kw):
        calls.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(local_review.subprocess, "run", spy)
    return calls


# ---- base ref resolution ----


def test_explicit_base_wins(repo: Path) -> None:
    git(repo, "branch", "other", "main")
    assert resolve_base_ref(repo, "other", "main") == "other"


def test_config_base_used_when_no_explicit(repo: Path) -> None:
    git(repo, "branch", "other", "main")
    assert resolve_base_ref(repo, None, "other") == "other"


def test_explicit_base_missing_does_not_fall_through(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="nope"):
        resolve_base_ref(repo, "nope", "main")


def test_config_base_missing_does_not_fall_through(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="nope"):
        resolve_base_ref(repo, None, "nope")


def test_origin_head_used_when_present(repo: Path) -> None:
    git(repo, "update-ref", "refs/remotes/origin/trunk", "main")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert resolve_base_ref(repo) == "origin/trunk"


def test_dangling_origin_head_falls_through_to_main(repo: Path) -> None:
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
    assert resolve_base_ref(repo) == "main"


def test_falls_back_to_main(repo: Path) -> None:
    assert resolve_base_ref(repo) == "main"


def test_nothing_resolves_lists_tried(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-b", "dev")
    commit(r, "a")
    with pytest.raises(LocalReviewError, match=r"tried:.*origin/HEAD.*main"):
        resolve_base_ref(r)


def test_compute_merge_base(repo: Path) -> None:
    assert compute_merge_base(repo, "main") == git(repo, "rev-parse", "main")


def test_compute_merge_base_unrelated(repo: Path) -> None:
    git(repo, "checkout", "--orphan", "orph")
    commit(repo, "o.txt")
    with pytest.raises(LocalReviewError, match="no merge base"):
        compute_merge_base(repo, "main")


# ---- preconditions ----


def test_happy_path(repo: Path) -> None:
    t = check_preconditions(repo)
    assert t.branch == "feature"
    assert t.base_ref == "main"
    assert t.base_sha == git(repo, "rev-parse", "main")
    assert t.head_sha == git(repo, "rev-parse", "HEAD")


def test_not_toplevel(repo: Path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    with pytest.raises(LocalReviewError, match="toplevel"):
        check_preconditions(sub)


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(LocalReviewError, match="not inside a git repository"):
        check_preconditions(tmp_path)


def test_detached_head(repo: Path) -> None:
    git(repo, "checkout", "--detach")
    with pytest.raises(LocalReviewError, match="check out a branch"):
        check_preconditions(repo)


def test_dirty_tracked_blocks(repo: Path) -> None:
    (repo / "a.txt").write_text("changed")
    with pytest.raises(LocalReviewError, match=r"a\.txt.*not reviewed"):
        check_preconditions(repo)


def test_dirty_names_only_first_five(repo: Path) -> None:
    for i in range(7):
        commit(repo, f"f{i}.txt")
        (repo / f"f{i}.txt").write_text("changed")
    with pytest.raises(LocalReviewError, match=r"and 2 more") as ei:
        check_preconditions(repo)
    assert ei.value.args[0].count(".txt") == 5


def test_untracked_does_not_block(repo: Path) -> None:
    (repo / "new.txt").write_text("u")
    assert check_preconditions(repo).branch == "feature"


def test_explicit_bad_base(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="nope"):
        check_preconditions(repo, base="nope")


def test_nothing_to_review_on_base_branch(repo: Path) -> None:
    git(repo, "checkout", "main")
    with pytest.raises(NothingToReviewError, match="Nothing to review: main has no commits beyond main"):
        check_preconditions(repo)


def test_nothing_to_review_no_new_commits(repo: Path) -> None:
    git(repo, "checkout", "-b", "same", "main")
    with pytest.raises(NothingToReviewError, match="same has no commits beyond main"):
        check_preconditions(repo)


def test_nothing_to_review_is_not_an_error() -> None:
    assert not issubclass(NothingToReviewError, LocalReviewError)
    assert not issubclass(LocalReviewError, NothingToReviewError)


def test_skip_review_allows_nothing_to_review(repo: Path) -> None:
    git(repo, "checkout", "main")
    t = check_preconditions(repo, review_phase=False)
    assert t.base_sha == t.head_sha


# ---- output dir ----


def test_output_dir_inside_repo_refused(repo: Path) -> None:
    with pytest.raises(LocalReviewError, match="inside the reviewed repo"):
        check_preconditions(repo, output_root=repo / "out")


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


def test_output_dir_outside_ok(repo: Path, tmp_path: Path) -> None:
    check_output_dir_outside_repo(repo, tmp_path / "out")
    assert check_preconditions(repo, output_root=tmp_path / "out").branch == "feature"
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


def test_no_network_git_commands(repo: Path, git_calls: list[list[str]]) -> None:
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
    git_calls.clear()
    check_preconditions(repo)
    subcommands = {c[1] for c in git_calls}
    assert subcommands
    assert subcommands.isdisjoint({"fetch", "pull", "remote", "push", "clone", "ls-remote"})
    assert all(c[0] == "git" for c in git_calls)


# ---- diff hooks and is_ancestor ----


def _env() -> dict:
    # Ignore the developer's global git config (e.g. color.diff=always).
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def test_get_changed_files_rev_range_merge_base(repo: Path) -> None:
    base = compute_merge_base(repo, "main")
    git(repo, "checkout", "main")
    commit(repo, "main-only.txt")
    git(repo, "checkout", "feature")
    commit(repo, "c.txt")
    files = get_changed_files("main", str(repo), _env(), rev_range=f"{base}..HEAD")
    assert sorted(files) == ["b.txt", "c.txt"]


def test_get_file_diff_rev_range_two_args(repo: Path) -> None:
    base = compute_merge_base(repo, "main")
    diff = get_file_diff("b.txt", "main", str(repo), _env(), rev_range=f"{base} HEAD")
    assert "+++ b/b.txt" in diff
    assert "+x" in diff
    assert get_file_diff("a.txt", "main", str(repo), _env(), rev_range=f"{base} HEAD") == ""


def test_default_range_still_origin_triple_dot(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    real = subprocess.Popen

    def spy(args, *a, **kw):
        seen.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(common.subprocess, "Popen", spy)
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    assert get_changed_files("main", str(repo), _env()) == ["b.txt"]
    assert "+x" in get_file_diff("b.txt", "main", str(repo), _env())
    diffs = [c for c in seen if c[:2] == ["git", "diff"]]
    assert ["git", "diff", "--name-only", "origin/main...HEAD"] in diffs
    assert ["git", "diff", "origin/main...HEAD", "--", "b.txt"] in diffs


def test_is_ancestor(repo: Path) -> None:
    base = compute_merge_base(repo, "main")
    head = git(repo, "rev-parse", "HEAD")
    assert is_ancestor(base, head, str(repo), _env()) is True
    assert is_ancestor(head, base, str(repo), _env()) is False


def test_git_timeout_becomes_failed_process(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **_kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(local_review.subprocess, "run", boom)
    proc = local_review._git(repo, "status")
    assert proc.returncode == 124
    assert "timed out" in proc.stderr
    assert "git status failed" in (local_review._guard_violation(repo, "abc") or "")
