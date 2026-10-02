"""Tests for the local-review address phase (real temporary git repos, stub backend)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

import pytest
from click.testing import CliRunner

from harness.backend import Backend
from harness.cli import cli
from harness.config import HarnessConfig, HarnessSection, LocalReviewConfig, RepoConfig
from harness.runners import local_review as lr
from harness.runners.local_review import LocalReviewError, LocalReviewTarget, read_manifest, review_dir_for

ADDRESS_TEXT = "ADDRESS INSTRUCTIONS"
DATES = ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", "2020-01-03T00:00:00Z")


def git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    r = subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return r.stdout.strip()


def commit_file(repo: Path, name: str, content: str, date: str | None = None) -> str:
    path = repo / name
    path.write_text(path.read_text() + content if path.exists() else content)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"change {name}", date=date)
    return git(repo, "rev-parse", "HEAD")


def block(title: str, line: int = 1, status: str = "open", file: str = "a.py") -> str:
    return (
        f"## Finding: {title}\n- severity: P1\n- file: {file}\n- line: {line}\n- status: {status}\n\nBody of {title}."
    )


def stamped(*blocks: str, kind: str = "file") -> str:
    return lr.stamp_findings("\n\n".join(blocks) + "\n", kind).text


@dataclass
class Env:
    cfg: HarnessConfig
    target: LocalReviewTarget
    root: Path
    repo: Path
    shas: list[str]
    kd: Path

    def write_review(
        self,
        sha: str | None = None,
        files: dict[str, str] | None = None,
        design: str | None = None,
        branch: str | None = None,
        file_status: str = "done",
    ) -> Path:
        sha = sha or self.target.head_sha
        branch = branch or self.target.branch
        rdir = review_dir_for(self.root, self.cfg.repo_slug, branch, sha)
        manifest: dict = {
            "version": 1,
            "branch": branch,
            "base_ref": self.target.base_ref,
            "base_sha": self.target.base_sha,
            "head_sha": sha,
            "files": {},
            "summary": "done",
        }
        for path, text in (files or {}).items():
            out = rdir / "files" / f"{path}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            manifest["files"][path] = file_status
        if design is not None:
            (rdir / "design.md").write_text(design, encoding="utf-8")
            manifest["design"] = "done"
        rdir.mkdir(parents=True, exist_ok=True)
        lr.write_manifest(rdir, manifest)
        return rdir

    def collection(self, rdir: Path) -> lr.FindingCollection:
        return lr.collect_findings(rdir)


def build_env(tmp_path: Path, dates: tuple[str, ...] = DATES, with_address_template: bool = True) -> Env:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    git(repo, "init", "-b", "main")
    commit_file(repo, "base.txt", "b\n", date=DATES[0])
    git(repo, "checkout", "-b", "feature/x")
    shas = [commit_file(repo, "a.py", "print(1)\n", date=dates[0])]
    for i, date in enumerate(dates[1:], start=2):
        shas.append(commit_file(repo, "a.py", f"print({i})\n", date=date))
    kd = tmp_path / "knowledge"
    (kd / "pr-review").mkdir(parents=True)
    for name in lr.REVIEW_TEMPLATES:
        (kd / "pr-review" / name).write_text("OUT {OUTPUT_FILE}", encoding="utf-8")
    if with_address_template:
        (kd / "pr-review" / lr.ADDRESS_TEMPLATE).write_text(ADDRESS_TEXT, encoding="utf-8")
    cfg = HarnessConfig(
        harness=HarnessSection(knowledge_dir=kd),
        repo=RepoConfig(name="o/r", working_dir=repo, name_provided=False),
        local_review=LocalReviewConfig(output_dir=str(tmp_path / "out")),
    )
    root = tmp_path / "out"
    target = lr.check_preconditions(repo, output_root=root, review_phase=False)
    return Env(cfg, target, root, repo, shas, kd)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return build_env(tmp_path)


Script = Callable[[str, Path, Path], "subprocess.CompletedProcess[str] | None"]


class StubBackend:
    """Test double: per finding, runs `script(finding_id, repo, resolution_path)`.

    The default script declines. The script may commit, write the resolution file, or raise.
    """

    def __init__(self, script: Script | None = None) -> None:
        self.script = script
        self.prompts: list[str] = []
        self.contexts: list[str] = []

    def run(self, prompt, cwd, opencode_dir=None, context=None):
        self.prompts.append(prompt)
        self.contexts.append(context or "")
        match = re.search(r"resolution file \(absolute path[^)]*\): (\S+)", prompt)
        assert match, prompt
        resolution = Path(match.group(1))
        finding = (context or "").rsplit(" ", 1)[-1]
        if self.script:
            res = self.script(finding, Path(cwd), resolution)
            if res is not None:
                return res
        else:
            decline(finding, Path(cwd), resolution)
        return subprocess.CompletedProcess([], 0, "", "")


def output_path(prompt: str) -> Path:
    match = re.search(r"(/\S+\.md)", prompt)
    assert match
    return Path(match.group(1))


def decline(finding: str, repo: Path, resolution: Path) -> None:
    resolution.write_text("decision: declined\nNot a real problem.\n", encoding="utf-8")


def fix(finding: str, repo: Path, resolution: Path) -> None:
    commit_file(repo, "a.py", f"# fix {finding}\n")
    resolution.write_text("decision: fixed\nChanged it.\n", encoding="utf-8")


def addr(env: Env, rdir: Path, backend: StubBackend):
    coll = env.collection(rdir)
    result = lr.address_findings(env.cfg, env.target, rdir, coll, backend=cast(Backend, backend))
    return coll, result


def status_of(rdir: Path, path: str, fid: str) -> str | None:
    return lr.read_finding_status(rdir / "files" / f"{path}.md", fid)


# ---------------------------------------------------------------------------
# FR-T1: flag-combination usage errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,message",
    [
        (["--skip-review"], "--skip-review requires --address"),
        (["--review-dir", "x"], "--review-dir requires --address"),
        (["--address", "--review-dir", "x"], "--review-dir requires --skip-review"),
    ],
)
def test_flag_combinations_are_usage_errors(minimal_toml: Path, args: list[str], message: str) -> None:
    r = CliRunner().invoke(cli, ["run", "--config", str(minimal_toml), "local-review", *args])
    assert r.exit_code == 2
    assert message in r.output


def test_address_usage_error_helper_and_runner_entry(env: Env) -> None:
    assert lr.address_usage_error(False, True, None) == "--skip-review requires --address"
    assert lr.address_usage_error(False, False, "d") == "--review-dir requires --address"
    assert lr.address_usage_error(True, False, "d") == "--review-dir requires --skip-review"
    assert lr.address_usage_error(True, False, None) is None
    assert lr.address_usage_error(True, True, None) is None
    assert lr.address_usage_error(True, True, "d") is None
    with pytest.raises(LocalReviewError, match="--skip-review requires --address"):
        lr.run_local_review(env.cfg, skip_review=True)


# ---------------------------------------------------------------------------
# FR-T2: review directory selection
# ---------------------------------------------------------------------------


def test_explicit_review_dir_head_and_ancestor_are_valid(env: Env) -> None:
    head = env.write_review(env.shas[2])
    old = env.write_review(env.shas[0])
    assert lr.select_review_dir(env.cfg, env.target, env.root, review_dir=head, skip_review=True) == head
    assert lr.select_review_dir(env.cfg, env.target, env.root, review_dir=old, skip_review=True) == old


def test_explicit_review_dir_without_manifest_is_an_error(env: Env, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(LocalReviewError, match=r"manifest\.json"):
        lr.select_review_dir(env.cfg, env.target, env.root, review_dir=empty, skip_review=True)
    with pytest.raises(LocalReviewError, match=r"manifest\.json"):
        lr.select_review_dir(env.cfg, env.target, env.root, review_dir=tmp_path / "missing", skip_review=True)


def test_explicit_review_dir_for_other_branch_is_an_error(env: Env) -> None:
    other = env.write_review(env.shas[2], branch="other")
    with pytest.raises(LocalReviewError, match="branch"):
        lr.select_review_dir(env.cfg, env.target, env.root, review_dir=other, skip_review=True)


def test_explicit_review_dir_not_an_ancestor_is_an_error(env: Env) -> None:
    git(env.repo, "checkout", "-b", "side", "main")
    side = commit_file(env.repo, "side.txt", "s\n", date=DATES[2])
    git(env.repo, "checkout", "feature/x")
    rdir = env.write_review(side)  # branch name matches, but the commit is not in HEAD's history
    with pytest.raises(LocalReviewError, match="not HEAD or an ancestor"):
        lr.select_review_dir(env.cfg, env.target, env.root, review_dir=rdir, skip_review=True)


def test_explicit_review_dir_of_a_descendant_commit_is_an_error(env: Env) -> None:
    git(env.repo, "checkout", "--detach", "HEAD")
    later = commit_file(env.repo, "a.py", "# later\n", date="2020-02-01T00:00:00Z")
    git(env.repo, "checkout", "feature/x")
    rdir = env.write_review(later)
    with pytest.raises(LocalReviewError, match="not HEAD or an ancestor"):
        lr.select_review_dir(env.cfg, env.target, env.root, review_dir=rdir, skip_review=True)


def test_without_skip_review_uses_directory_of_current_head(env: Env) -> None:
    head = env.write_review(env.shas[2])
    env.write_review(env.shas[1])
    assert lr.select_review_dir(env.cfg, env.target, env.root) == head
    assert lr.select_review_dir(env.cfg, env.target, env.root, reviewed_dir=head) == head


def test_without_skip_review_missing_head_directory_is_an_error(env: Env) -> None:
    env.write_review(env.shas[1])  # an older review is not accepted without --skip-review
    with pytest.raises(LocalReviewError, match="no completed review"):
        lr.select_review_dir(env.cfg, env.target, env.root)


def _set_mtime(rdir: Path, when: float) -> None:
    os.utime(rdir / lr.MANIFEST_NAME, (when, when))


def test_skip_review_picks_newest_commit_date_not_newest_mtime(env: Env) -> None:
    older = env.write_review(env.shas[0])
    newer = env.write_review(env.shas[1])
    _set_mtime(older, 2_000_000_000)
    _set_mtime(newer, 1_000_000_000)
    assert lr.select_review_dir(env.cfg, env.target, env.root, skip_review=True) == newer


def test_skip_review_prefers_head_directory_when_it_is_newest(env: Env) -> None:
    env.write_review(env.shas[0])
    head = env.write_review(env.shas[2])
    assert lr.select_review_dir(env.cfg, env.target, env.root, skip_review=True) == head


def test_skip_review_tie_on_commit_date_is_broken_by_manifest_mtime(tmp_path: Path) -> None:
    same = "2020-05-05T00:00:00Z"
    env = build_env(tmp_path, dates=(same, same, same))
    first = env.write_review(env.shas[0])
    second = env.write_review(env.shas[1])
    _set_mtime(first, 2_000_000_000)
    _set_mtime(second, 1_000_000_000)
    assert lr.select_review_dir(env.cfg, env.target, env.root, skip_review=True) == first
    _set_mtime(first, 1_000_000_000)
    _set_mtime(second, 2_000_000_000)
    assert lr.select_review_dir(env.cfg, env.target, env.root, skip_review=True) == second


def test_skip_review_ignores_non_ancestors_corrupt_and_other_branch_reviews(env: Env) -> None:
    git(env.repo, "checkout", "-b", "side", "main")
    side = commit_file(env.repo, "side.txt", "s\n", date="2021-01-01T00:00:00Z")
    git(env.repo, "checkout", "feature/x")
    env.write_review(side)  # newest commit date, but not in HEAD's history
    env.write_review(env.shas[2], branch="other")  # another branch
    bad = env.write_review(env.shas[1])
    (bad / lr.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    good = env.write_review(env.shas[0])
    assert lr.select_review_dir(env.cfg, env.target, env.root, skip_review=True) == good


def test_skip_review_without_any_review_tells_user_to_run_local_review(env: Env) -> None:
    with pytest.raises(LocalReviewError, match="run `harness run local-review` first"):
        lr.select_review_dir(env.cfg, env.target, env.root, skip_review=True)
    env.write_review(env.shas[0], branch="other")
    with pytest.raises(LocalReviewError, match="run `harness run local-review` first"):
        lr.select_review_dir(env.cfg, env.target, env.root, skip_review=True)


# ---------------------------------------------------------------------------
# FR-T3: findings handling
# ---------------------------------------------------------------------------


def test_collect_counts_open_skipped_and_unparseable(env: Env) -> None:
    bad = block("Bad severity").replace("severity: P1", "severity: P9")
    text = stamped(block("one", 5), block("two", 2), block("won't", 3, status="wontfix"))
    text += "\n" + bad + "\n\n" + block("unstamped", 4, status="fixed")
    rdir = env.write_review(files={"a.py": text})
    coll = env.collection(rdir)
    assert [f.block.title for f in coll.open] == ["two", "one"]  # ordered by line
    assert coll.skipped_not_open == 1  # the unstamped block counts as unparseable, not skipped
    assert coll.unparseable == 2
    assert all(f.id.startswith("file-") for f in coll.open)


def test_collect_rejects_path_traversal_ids(env: Env) -> None:
    evil = block("evil").replace("- status: open", "- status: open\n- id: ../escape")
    rdir = env.write_review(files={"a.py": evil})
    coll = env.collection(rdir)
    assert coll.open == []
    assert coll.unparseable == 1


def test_collect_reads_only_done_passes_and_orders_design_last(env: Env) -> None:
    f1 = stamped(block("zz", 1, file="b.py"), kind="file")
    f2 = stamped(block("aa", 9, file="a.py"), kind="file")
    design = stamped(block("dd", 1), kind="design")
    rdir = env.write_review(files={"b.py": f1, "a.py": f2}, design=design)
    manifest = read_manifest(rdir)
    assert manifest is not None
    manifest["files"]["b.py"] = "failed"
    lr.write_manifest(rdir, manifest)
    coll = env.collection(rdir)
    assert [(f.pass_name, f.block.title) for f in coll.open] == [("file", "aa"), ("design", "dd")]


def test_collect_without_manifest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(LocalReviewError, match=r"manifest\.json"):
        lr.collect_findings(tmp_path)


def test_only_open_findings_reach_the_backend(env: Env) -> None:
    text = stamped(block("open", 1), block("skip", 2, status="wontfix"), block("old", 3, status="declined"))
    rdir = env.write_review(files={"a.py": text})
    backend = StubBackend()
    coll, result = addr(env, rdir, backend)
    assert len(backend.contexts) == 1
    assert coll.skipped_not_open == 2
    assert result.count("declined") == 1


def test_unparseable_block_makes_exit_non_zero_even_without_failures(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = block("Bad").replace("severity: P1", "severity: P9")
    rdir = env.write_review(files={"a.py": stamped(block("ok")) + "\n" + bad + "\n"})
    coll = env.collection(rdir)
    ok = lr.run_address_phase(env.cfg, env.target, rdir, coll, backend=cast(Backend, StubBackend()))
    assert ok is False
    assert "1 declined, 0 failed, 0 skipped (not open), 1 unparseable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# FR-T4: step 4 table (one finding per case)
# ---------------------------------------------------------------------------


def two_commits(finding: str, repo: Path, resolution: Path) -> None:
    commit_file(repo, "a.py", "# one\n")
    fix(finding, repo, resolution)


def fixed_no_commit(finding: str, repo: Path, resolution: Path) -> None:
    resolution.write_text("decision: fixed\n", encoding="utf-8")


def declined_but_committed(finding: str, repo: Path, resolution: Path) -> None:
    commit_file(repo, "a.py", "# oops\n")
    decline(finding, repo, resolution)


def no_resolution(finding: str, repo: Path, resolution: Path) -> None:
    return None


def commit_without_resolution(finding: str, repo: Path, resolution: Path) -> None:
    commit_file(repo, "a.py", "# oops\n")


def bad_first_line(finding: str, repo: Path, resolution: Path) -> None:
    resolution.write_text("Fixed it.\ndecision: declined\n", encoding="utf-8")


def mixed_case_decision(finding: str, repo: Path, resolution: Path) -> None:
    resolution.write_text("  Decision: Declined \n", encoding="utf-8")


def untracked_file_is_ignored(finding: str, repo: Path, resolution: Path) -> None:
    (repo / "scratch.txt").write_text("untracked")
    decline(finding, repo, resolution)


def merge_commit(finding: str, repo: Path, resolution: Path) -> None:
    git(repo, "checkout", "-b", "tmp")
    commit_file(repo, "c.py", "c\n")
    git(repo, "checkout", "feature/x")
    git(repo, "merge", "--no-ff", "-m", "merge", "tmp")
    resolution.write_text("decision: fixed\n", encoding="utf-8")


@pytest.mark.parametrize(
    "action,outcome,new_status",
    [
        (fix, "fixed", "fixed"),
        (decline, "declined", "declined"),
        (two_commits, "failed", "open"),
        (fixed_no_commit, "failed", "open"),
        (declined_but_committed, "failed", "open"),
        (no_resolution, "failed", "open"),
        (commit_without_resolution, "failed", "open"),
        (bad_first_line, "failed", "open"),
        (mixed_case_decision, "declined", "declined"),
        (untracked_file_is_ignored, "declined", "declined"),
        (merge_commit, "failed", "open"),
    ],
)
def test_step4_table(env: Env, action: Script, outcome: str, new_status: str) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"))})
    (fid,) = [f.id for f in env.collection(rdir).open]
    pre = git(env.repo, "rev-parse", "HEAD")
    backend = StubBackend(action)
    _, result = addr(env, rdir, backend)
    assert result.outcomes == {fid: outcome}
    assert not result.stopped
    assert status_of(rdir, "a.py", fid) == new_status
    text = (rdir / "files" / "a.py.md").read_text()
    if outcome == "fixed":
        head = git(env.repo, "rev-parse", "HEAD")
        assert git(env.repo, "rev-list", "--parents", "-n1", "HEAD").split() == [head, pre]
        assert f"- status: fixed\n- commit: {head}\n" in text
    else:
        assert "- commit:" not in text
    if outcome == "failed":
        assert "- status: open" in text


def test_two_fixed_findings_each_get_a_commit_line_and_keep_the_rest_of_the_file(env: Env) -> None:
    text = stamped(block("one"), block("two", 2))
    rdir = env.write_review(files={"a.py": text})
    first, second = (f.id for f in env.collection(rdir).open)
    _, result = addr(env, rdir, StubBackend(cast(Script, fix)))
    assert result.outcomes == {first: "fixed", second: "fixed"}
    new = (rdir / "files" / "a.py.md").read_text()
    assert new.count("- commit: ") == 2
    assert new.count("Body of one.") == 1
    assert lr.read_finding_status(rdir / "files" / "a.py.md", first) == "fixed"


def test_stale_resolution_file_is_deleted_before_the_call(env: Env) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"))})
    (fid,) = [f.id for f in env.collection(rdir).open]
    stale = rdir / lr.RESOLUTIONS_DIR / f"{fid}.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("decision: declined\nleft over\n", encoding="utf-8")
    _, result = addr(env, rdir, StubBackend(cast(Script, no_resolution)))
    assert result.outcomes == {fid: "failed"}
    assert not stale.exists()


def test_backend_nonzero_exit_timeout_and_exception_fail_the_finding(env: Env) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"), block("two", 2), block("three", 3), block("four", 4))})
    ids = [f.id for f in env.collection(rdir).open]
    calls = {"n": 0}

    def script(finding: str, repo: Path, resolution: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess([], 1, "", "boom")
        if calls["n"] == 2:
            raise subprocess.TimeoutExpired("x", 1)
        if calls["n"] == 3:
            raise RuntimeError("kaput")
        decline(finding, repo, resolution)
        return None

    _, result = addr(env, rdir, StubBackend(script))
    assert [result.outcomes[i] for i in ids] == ["failed", "failed", "failed", "declined"]
    assert not result.stopped  # these failures do not stop the phase
    assert status_of(rdir, "a.py", ids[0]) == "open"


def test_backend_nonzero_exit_with_dirty_tree_still_stops_the_phase(env: Env) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"), block("two", 2))})

    def script(finding: str, repo: Path, resolution: Path):
        (repo / "a.py").write_text("dirty\n")
        return subprocess.CompletedProcess([], 1, "", "boom")

    backend = StubBackend(script)
    _, result = addr(env, rdir, backend)
    assert result.stopped
    assert len(backend.contexts) == 1


# ---------------------------------------------------------------------------
# FR-T5: stop-the-phase conditions
# ---------------------------------------------------------------------------


def rewrite_history(finding: str, repo: Path, resolution: Path) -> None:
    git(repo, "reset", "--hard", "HEAD~1")
    resolution.write_text("decision: declined\n", encoding="utf-8")


def rewrite_with_new_commit(finding: str, repo: Path, resolution: Path) -> None:
    git(repo, "reset", "--hard", "HEAD~1")
    commit_file(repo, "a.py", "# different\n")
    resolution.write_text("decision: fixed\n", encoding="utf-8")


def dirty_tracked(finding: str, repo: Path, resolution: Path) -> None:
    (repo / "a.py").write_text("dirty\n")
    resolution.write_text("decision: declined\n", encoding="utf-8")


def dirty_after_commit(finding: str, repo: Path, resolution: Path) -> None:
    fix(finding, repo, resolution)
    (repo / "a.py").write_text("dirty\n")


def switch_branch(finding: str, repo: Path, resolution: Path) -> None:
    git(repo, "checkout", "-b", "elsewhere")
    resolution.write_text("decision: declined\n", encoding="utf-8")


def detach_head(finding: str, repo: Path, resolution: Path) -> None:
    git(repo, "checkout", "--detach")
    resolution.write_text("decision: declined\n", encoding="utf-8")


STOP_CASES = [
    (rewrite_history, "history rewritten"),
    (rewrite_with_new_commit, "history rewritten"),
    (dirty_tracked, "uncommitted changes"),
    (dirty_after_commit, "uncommitted changes"),
    (switch_branch, "branch changed"),
    (detach_head, "branch changed"),
]


@pytest.mark.parametrize("action,reason", STOP_CASES)
def test_stop_conditions_fail_finding_stop_and_reset_nothing(
    env: Env, action: Script, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"), block("two", 2), block("three", 3))})
    coll = env.collection(rdir)
    first = coll.open[0].id
    backend = StubBackend(action)
    head_before = git(env.repo, "rev-parse", "HEAD")
    ok = lr.run_address_phase(env.cfg, env.target, rdir, coll, backend=cast(Backend, backend))
    out = capsys.readouterr()
    assert ok is False
    assert len(backend.contexts) == 1  # no further backend calls
    assert status_of(rdir, "a.py", first) == "open"  # finding failed, status untouched
    assert "findings: 0 fixed, 0 declined, 1 failed, 2 skipped (not open), 0 unparseable" in out.out
    assert "ERROR: address phase stopped early" in out.err
    assert reason in out.err
    # nothing was repaired: the tree is as the backend left it
    if action in (rewrite_history, rewrite_with_new_commit):
        assert git(env.repo, "rev-parse", "HEAD") != head_before
    if action in (dirty_tracked, dirty_after_commit):
        assert git(env.repo, "status", "--porcelain", "--untracked-files=no") != ""
    if action is switch_branch:
        assert git(env.repo, "branch", "--show-current") == "elsewhere"
    if action is detach_head:
        assert git(env.repo, "branch", "--show-current") == ""


def test_stop_result_fields(env: Env) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"), block("two", 2), block("three", 3))})
    _, result = addr(env, rdir, StubBackend(cast(Script, dirty_tracked)))
    assert result.stopped
    assert result.not_reached == 2
    assert result.stop_reason is not None
    assert list(result.outcomes.values()) == ["failed"]


def test_dirty_tree_before_the_first_call_stops_with_no_backend_call(env: Env) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"), block("two", 2))})
    (env.repo / "a.py").write_text("user edit\n")
    backend = StubBackend()
    _, result = addr(env, rdir, backend)
    assert backend.contexts == []
    assert result.stopped
    assert "uncommitted changes" in (result.stop_reason or "")
    assert result.not_reached == 1


def test_wrong_branch_before_the_first_call_stops_with_no_backend_call(env: Env) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"))})
    git(env.repo, "checkout", "-b", "elsewhere")
    backend = StubBackend()
    _, result = addr(env, rdir, backend)
    assert backend.contexts == []
    assert "current branch" in (result.stop_reason or "")


# ---------------------------------------------------------------------------
# FR-T6: summary line; prompt contents
# ---------------------------------------------------------------------------


def test_summary_line_exact_format(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    text = stamped(
        block("a", 1),
        block("b", 2),
        block("c", 3),
        block("d", 4),
        block("e", 5),
        block("f", 6, status="wontfix"),
        block("g", 7, status="fixed"),
    )
    rdir = env.write_review(files={"a.py": text})
    actions = iter([fix, fix, decline, no_resolution, fix])

    def script(finding: str, repo: Path, resolution: Path):
        next(actions)(finding, repo, resolution)

    coll = env.collection(rdir)
    ok = lr.run_address_phase(env.cfg, env.target, rdir, coll, backend=cast(Backend, StubBackend(script)))
    out = capsys.readouterr().out
    assert "findings: 3 fixed, 1 declined, 1 failed, 2 skipped (not open), 0 unparseable\n" in out
    assert ok is False  # one failed finding


def test_summary_line_all_good_returns_true(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("a"))})
    coll = env.collection(rdir)
    assert lr.run_address_phase(env.cfg, env.target, rdir, coll, backend=cast(Backend, StubBackend())) is True
    assert "findings: 0 fixed, 1 declined, 0 failed, 0 skipped (not open), 0 unparseable" in capsys.readouterr().out


def test_prompt_contents(env: Env) -> None:
    rdir = env.write_review(env.shas[1], files={"a.py": stamped(block("one", 3))})
    coll = env.collection(rdir)
    (finding,) = coll.open
    backend = StubBackend()
    lr.address_findings(env.cfg, env.target, rdir, coll, backend=cast(Backend, backend))
    (prompt,) = backend.prompts
    assert prompt.startswith(ADDRESS_TEXT)
    assert f"{lr.ADDRESS_HEADING}" in prompt
    assert f"- finding id: {finding.id}" in prompt
    assert f"- review head sha (12 chars): {env.shas[1][:12]}" in prompt
    assert "- file: a.py" in prompt
    assert "- line: 3" in prompt
    assert str((rdir / "resolutions" / f"{finding.id}.md").resolve()) in prompt
    assert finding.block.text.strip() in prompt
    assert "```diff" in prompt
    assert "+print(1)" in prompt  # diff of a.py against the merge base
    assert backend.contexts == [f"local-review address {finding.id}"]


def test_prompt_diff_is_capped_and_vibe_context_optional() -> None:
    finding = lr.AddressFinding("file-12345678", "file", Path("x.md"), lr.parse_findings(block("t"))[0])
    big = "x" * (lr.MAX_DIFF_CHARS + 100)
    prompt = lr.build_address_prompt("INSTR", finding, Path("/r.md"), "a" * 40, big)
    assert lr.TRUNCATED_MARKER in prompt
    assert "x" * (lr.MAX_DIFF_CHARS + 1) not in prompt
    assert "vibe-heal" not in prompt
    with_ctx = lr.build_address_prompt("INSTR", finding, Path("/r.md"), "a" * 40, "d", "SONAR SAYS")
    assert "SONAR SAYS" in with_ctx


# ---------------------------------------------------------------------------
# write_finding_status helper
# ---------------------------------------------------------------------------


def test_write_finding_status_changes_only_the_status_line(tmp_path: Path) -> None:
    text = stamped(block("one"), block("two", 2))
    path = tmp_path / "f.md"
    path.write_text(text, encoding="utf-8")
    fid = lr.parse_findings(text)[0].id or ""
    lr.write_finding_status(path, fid, "wontfix")
    assert path.read_text() == text.replace("- status: open", "- status: wontfix", 1)
    lr.write_finding_status(path, fid, "fixed", commit="abc")
    lr.write_finding_status(path, fid, "fixed", commit="def")
    new = path.read_text()
    assert new.count("- commit: def") == 1
    assert "abc" not in new
    with pytest.raises(ValueError, match="status must be"):
        lr.write_finding_status(path, fid, "failed")
    with pytest.raises(LocalReviewError, match="not found"):
        lr.write_finding_status(path, "file-00000000", "fixed")


# ---------------------------------------------------------------------------
# run_local_review: wiring and exit codes
# ---------------------------------------------------------------------------


def test_run_local_review_skip_review_success(env: Env, tmp_xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rdir = env.write_review(files={"a.py": stamped(block("one"), block("two", 2, status="wontfix"))})
    backend = StubBackend(cast(Script, fix))
    code = lr.run_local_review(env.cfg, address=True, skip_review=True, backend=cast(Backend, backend))
    out = capsys.readouterr().out
    assert code == 0
    assert f"Addressing review: {rdir}" in out
    assert "Findings to address: 1 open, 1 skipped (not open), 0 unparseable" in out
    assert "findings: 1 fixed, 0 declined, 0 failed, 1 skipped (not open), 0 unparseable" in out
    assert len(backend.contexts) == 1  # no review passes ran


def test_run_local_review_failed_finding_exits_one(env: Env, tmp_xdg: Path) -> None:
    env.write_review(files={"a.py": stamped(block("one"))})
    backend = StubBackend(cast(Script, no_resolution))
    assert lr.run_local_review(env.cfg, address=True, skip_review=True, backend=cast(Backend, backend)) == 1


def test_run_local_review_stopped_phase_exits_one(env: Env, tmp_xdg: Path) -> None:
    env.write_review(files={"a.py": stamped(block("one"))})
    backend = StubBackend(cast(Script, switch_branch))
    assert lr.run_local_review(env.cfg, address=True, skip_review=True, backend=cast(Backend, backend)) == 1


def test_run_local_review_no_prior_review_exits_one(
    env: Env, tmp_xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = lr.run_local_review(env.cfg, address=True, skip_review=True, backend=cast(Backend, StubBackend()))
    assert code == 1
    assert "run `harness run local-review` first" in capsys.readouterr().err


def test_run_local_review_missing_address_template_exits_one(
    tmp_path: Path, tmp_xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = build_env(tmp_path, with_address_template=False)
    env.write_review(files={"a.py": stamped(block("one"))})
    code = lr.run_local_review(env.cfg, address=True, skip_review=True, backend=cast(Backend, StubBackend()))
    assert code == 1
    assert lr.ADDRESS_TEMPLATE in capsys.readouterr().err


def test_run_local_review_explicit_review_dir(env: Env, tmp_xdg: Path) -> None:
    old = env.write_review(env.shas[0], files={"a.py": stamped(block("old"))})
    env.write_review(env.shas[1], files={"a.py": stamped(block("newer"))})
    backend = StubBackend()
    code = lr.run_local_review(env.cfg, address=True, skip_review=True, review_dir=old, backend=cast(Backend, backend))
    assert code == 0
    assert len(backend.contexts) == 1
    assert (old / "resolutions").is_dir()


def test_run_local_review_with_review_phase_addresses_the_fresh_review(env: Env, tmp_xdg: Path) -> None:
    class Both(StubBackend):
        def run(self, prompt, cwd, opencode_dir=None, context=None):
            if (context or "").startswith("local-review address"):
                return super().run(prompt, cwd, opencode_dir, context)
            out = output_path(prompt)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(block("fresh") + "\n" if "files" in str(out) else "S\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")

    backend = Both(cast(Script, fix))
    code = lr.run_local_review(env.cfg, address=True, backend=cast(Backend, backend))
    assert code == 0
    assert len([c for c in backend.contexts if c.startswith("local-review address")]) == 1
    rdir = review_dir_for(env.root, env.cfg.repo_slug, env.target.branch, env.target.head_sha)
    assert "- status: fixed\n- commit: " in (rdir / "files" / "a.py.md").read_text()


# ---------------------------------------------------------------------------
# FR-T7 / NFR-1: full command, no remotes, no gh on PATH
# ---------------------------------------------------------------------------


class CliStub:
    """Replaces `harness.runners.local_review.Backend`: reviews, then fixes one finding and declines another."""

    calls: ClassVar[list[str]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def run(self, prompt, cwd, opencode_dir=None, context=None):
        label = context or ""
        CliStub.calls.append(label)
        cwd_path = Path(cwd)
        if label.startswith("local-review address"):
            match = re.search(r"resolution file \(absolute path[^)]*\): (\S+)", prompt)
            assert match
            resolution = Path(match.group(1))
            if len(CliStub.calls) == 4:  # first address call
                fix("x", cwd_path, resolution)
            else:
                decline("x", cwd_path, resolution)
            return subprocess.CompletedProcess([], 0, "", "")
        out = output_path(prompt)
        out.parent.mkdir(parents=True, exist_ok=True)
        if label.startswith("local-review file"):
            out.write_text(block("file finding", 1) + "\n", encoding="utf-8")
        elif label.endswith("design"):
            out.write_text(block("design finding", 1) + "\n\n## Summary\nok\n", encoding="utf-8")
        else:
            out.write_text("A summary.\n", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")


def test_full_command_without_remotes_or_gh(
    env: Env, tmp_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert git(env.repo, "remote") == ""
    # PATH holds only a symlink to git: no gh.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_path = shutil.which("git")
    assert git_path
    (bin_dir / "git").symlink_to(git_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert shutil.which("gh") is None
    cfg_path = tmp_path / "lr.toml"
    cfg_path.write_text(
        f'[harness]\nknowledge_dir = "{env.kd}"\n[repo]\nworking_dir = "{env.repo}"\n'
        f'[local_review]\noutput_dir = "{env.root}"\n'
    )
    CliStub.calls = []
    git_calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(args, *a, **kw):
        if isinstance(args, list) and args and args[0] == "git":
            git_calls.append([str(a) for a in args])
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr("harness.runners.local_review.Backend", CliStub)
    r = CliRunner().invoke(cli, ["run", "--config", str(cfg_path), "local-review", "--address"])
    assert r.exit_code == 0, r.output
    assert "findings: 1 fixed, 1 declined, 0 failed, 0 skipped (not open), 0 unparseable" in r.output
    assert git(env.repo, "log", "--oneline", "-n1").endswith("change a.py")
    assert git(env.repo, "rev-list", "--count", "main..HEAD") == str(len(env.shas) + 1)
    forbidden = {"fetch", "pull", "push", "clone", "ls-remote"}
    assert git_calls
    assert not [c for c in git_calls if forbidden & set(c)]
    assert git(env.repo, "remote") == ""
