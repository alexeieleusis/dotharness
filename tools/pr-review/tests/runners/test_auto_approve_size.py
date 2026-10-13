import os
import subprocess
from pathlib import Path

import pytest

from harness.config import AutoApproveConfig
from harness.runners.auto_approve_size import SizeEvaluation, evaluate_size


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=_ENV)  # noqa: S603, S607


_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _lines(n: int, prefix: str = "x") -> str:
    return "".join(f"{prefix}{i}\n" for i in range(n))


@pytest.fixture
def repo(tmp_path):
    """Repo with branch ``main`` mirrored as ``origin/main`` and a checked-out feature branch."""
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "old.txt").write_text(_lines(30, "keep"))
    (tmp_path / "gone.txt").write_text(_lines(5, "gone"))
    (tmp_path / "edit.txt").write_text(_lines(5, "e"))
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "checkout", "-b", "feature")
    return tmp_path


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")


def _eval(repo: Path, viewed: int = 75, approve: int = 500) -> SizeEvaluation:
    cfg = AutoApproveConfig(enabled=True, viewed_max_lines=viewed, approve_max_lines=approve)
    return evaluate_size("main", str(repo), _ENV, cfg)


def test_text_file_counted(repo):
    (repo / "new.txt").write_text(_lines(10))
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts == {"new.txt": 10}
    assert ev.total_lines == 10
    assert ev.small_files == ["new.txt"]
    assert ev.pr_is_small is True


def test_binary_deleted_renamed_not_viewed(repo):
    (repo / "bin.dat").write_bytes(b"\x00\x09\x08\x07")
    (repo / "gone.txt").unlink()
    _git(repo, "mv", "old.txt", "moved.txt")
    (repo / "edit.txt").write_text(_lines(5, "e") + "added\n")
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts == {"edit.txt": 1}
    assert ev.small_files == ["edit.txt"]
    # deleted gone.txt (5 lines) counts toward the total; binary contributes 0; pure rename 0
    assert ev.total_lines == 6


def test_renamed_with_edits_not_viewed_but_counted(repo):
    _git(repo, "mv", "old.txt", "moved.txt")
    (repo / "moved.txt").write_text(_lines(30, "keep").replace("keep3\n", "changed\n"))
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts == {}
    assert ev.small_files == []
    assert ev.total_lines == 2


def test_large_deletion_and_rename_edits_block_approval(repo):
    (repo / "big.txt").write_text(_lines(600, "big"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "big")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "mv", "big.txt", "moved.txt")
    (repo / "moved.txt").write_text(_lines(300, "new") + _lines(600, "big")[len(_lines(300, "big")) :])
    (repo / "edit.txt").write_text(_lines(5, "e") + "added\n")
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts == {"edit.txt": 1}
    assert ev.small_files == ["edit.txt"]
    assert ev.total_lines > 500  # ~600 changed lines in the rename, plus 1 counted line
    assert ev.pr_is_small is False


@pytest.mark.parametrize(("n", "small"), [(74, True), (75, False), (76, False)])
def test_file_boundary(repo, n, small):
    (repo / "f.txt").write_text(_lines(n))
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts["f.txt"] == n
    assert (ev.small_files == ["f.txt"]) is small


@pytest.mark.parametrize(("n", "small"), [(499, True), (500, False), (501, False)])
def test_pr_boundary(repo, n, small):
    (repo / "f.txt").write_text(_lines(n))
    _commit(repo)
    ev = _eval(repo)
    assert ev.total_lines == n
    assert ev.pr_is_small is small


def test_zero_thresholds_disable(repo):
    (repo / "f.txt").write_text(_lines(1))
    _commit(repo)
    ev = _eval(repo, viewed=0, approve=0)
    assert ev.small_files == []
    assert ev.pr_is_small is False


def test_lockfile_counted(repo):
    (repo / "package-lock.json").write_text(_lines(40, "dep"))
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts == {"package-lock.json": 40}
    assert ev.small_files == ["package-lock.json"]


def test_total_sums_across_files(repo):
    (repo / "a.txt").write_text(_lines(60))
    (repo / "b.txt").write_text(_lines(80))
    _commit(repo)
    ev = _eval(repo)
    assert ev.total_lines == 140
    assert ev.small_files == ["a.txt"]


def test_glob_like_filename_counted_literally(repo):
    (repo / "[draft].md").write_text(_lines(600))
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts == {"[draft].md": 600}
    assert ev.pr_is_small is False


def test_git_failure_aborts(repo, monkeypatch):
    from harness.runners import auto_approve_size as mod

    (repo / "big.txt").write_text(_lines(600))
    _commit(repo)

    def failing(cmd, *a, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(mod, "run_cmd", failing)
    with pytest.raises(subprocess.CalledProcessError):
        _eval(repo)


def test_removed_dash_comment_lines_are_counted(repo):
    """Removed ``-- comment`` lines diff as ``--- comment`` and must not be mistaken for headers."""
    (repo / "mig.sql").write_text(_lines(600, "-- c"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "sql")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "mig.sql").write_text("")
    _commit(repo)
    ev = _eval(repo)
    assert ev.file_counts == {"mig.sql": 600}
    assert ev.pr_is_small is False


def test_color_always_config_does_not_change_counts(repo):
    (repo / "new.txt").write_text(_lines(10))
    _commit(repo)
    env = {
        **_ENV,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "color.ui",
        "GIT_CONFIG_VALUE_0": "always",
        "GIT_CONFIG_KEY_1": "color.diff",
        "GIT_CONFIG_VALUE_1": "always",
    }
    cfg = AutoApproveConfig(enabled=True, viewed_max_lines=75, approve_max_lines=500)
    ev = evaluate_size("main", str(repo), env, cfg)
    assert ev.file_counts == {"new.txt": 10}
    assert ev.total_lines == 10
