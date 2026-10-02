"""Tests for the local-review finding format, id stamping, change context and prompts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import cast

import pytest

from harness.backend import Backend
from harness.config import HarnessConfig, HarnessSection, LocalReviewConfig, RepoConfig
from harness.runners import local_review as lr
from harness.runners.local_review import (
    LocalReviewError,
    LocalReviewTarget,
    RepoMutatedError,
    build_change_context,
    build_change_description,
    build_design_prompt,
    build_file_prompt,
    build_summary_prompt,
    finding_id,
    parse_findings,
    read_manifest,
    resolve_output_root,
    review_dir_for,
    stamp_findings,
    stamp_findings_file,
    write_manifest,
)

KNOWLEDGE = Path(__file__).resolve().parents[4] / "knowledge" / "pr-review"

BLOCK_A = """## Finding: Off by one
- severity: P0
- file: src/a.py
- line: 12
- status: open

Loop skips the last item for `[1]`. Use `<=`."""

BLOCK_B = """## Finding: Missing check
- severity: P1
- file: src/b.py
- line: 1
- status: open

No validation of `None`."""


def sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def test_stamp_id_is_pass_plus_sha256_prefix_of_original_block() -> None:
    res = stamp_findings(BLOCK_A + "\n\n" + BLOCK_B + "\n", "file")
    assert res.stamped == 2
    assert [b.id for b in res.blocks] == [f"file-{sha8(BLOCK_A)}", f"file-{sha8(BLOCK_B)}"]
    assert finding_id("design", BLOCK_A) == f"design-{sha8(BLOCK_A)}"
    assert f"- status: open\n- id: file-{sha8(BLOCK_A)}\n\nLoop" in res.text


def test_restamp_after_text_edit_keeps_ids() -> None:
    first = stamp_findings(BLOCK_A + "\n", "design")
    edited = first.text.replace("Use `<=`.", "Totally rewritten body.").replace("status: open", "status: wontfix")
    second = stamp_findings(edited, "design")
    assert second.stamped == 0
    assert second.already_stamped == 1
    assert second.blocks[0].id == first.blocks[0].id
    assert second.blocks[0].status == "wontfix"
    assert second.text == edited


def test_restamp_is_idempotent() -> None:
    first = stamp_findings(BLOCK_A + "\n" + "\n" + BLOCK_B, "file")
    assert stamp_findings(first.text, "file").text == first.text


@pytest.mark.parametrize("missing", ["severity", "file", "line", "status"])
def test_missing_required_field_unstamped_counted_and_warned(missing: str, caplog: pytest.LogCaptureFixture) -> None:
    bad = "\n".join(ln for ln in BLOCK_A.splitlines() if not ln.startswith(f"- {missing}:"))
    text = bad + "\n\n" + BLOCK_B + "\n"
    with caplog.at_level(logging.WARNING, logger=lr.logger.name):
        res = stamp_findings(text, "file")
    assert res.unparseable == 1
    assert res.stamped == 1
    assert bad in res.text
    assert "- id:" not in res.blocks[0].text
    assert not res.blocks[0].valid
    assert "missing a required field" in caplog.text


@pytest.mark.parametrize(
    "old,new",
    [
        ("severity: P0", "severity: P2"),
        ("line: 12", "line: twelve"),
        ("status: open", "status: maybe"),
        ("file: src/a.py", "file: "),
    ],
)
def test_invalid_values_are_unparseable(old: str, new: str) -> None:
    res = stamp_findings(BLOCK_A.replace(old, new), "file")
    assert (res.stamped, res.unparseable) == (0, 1)


def test_summary_section_and_prose_are_not_findings_and_preserved() -> None:
    text = "intro\n\n" + BLOCK_A + "\n\n## Summary\nAll fine.\n"
    res = stamp_findings(text, "design")
    assert len(res.blocks) == 1
    assert res.text.endswith("\n\n## Summary\nAll fine.\n")
    assert res.text.startswith("intro\n\n")
    assert res.blocks[0].end_line <= res.text.splitlines().index("## Summary")


def test_heading_inside_code_fence_does_not_split_block() -> None:
    block = BLOCK_A + "\n\n```md\n## Finding: fake\n## Other\n```\nmore"
    res = stamp_findings(block, "file")
    assert len(res.blocks) == 1
    assert res.stamped == 1
    assert res.blocks[0].id == f"file-{sha8(block)}"  # hash over whole original block


def test_no_findings_and_empty_input() -> None:
    assert stamp_findings("", "file").blocks == []
    res = stamp_findings("No findings.\n", "file")
    assert (res.stamped, res.unparseable, res.text) == (0, 0, "No findings.\n")


def test_parse_fields() -> None:
    (blk,) = parse_findings(BLOCK_A)
    assert (blk.title, blk.severity, blk.file, blk.line, blk.status, blk.id, blk.valid) == (
        "Off by one",
        "P0",
        "src/a.py",
        12,
        "open",
        None,
        True,
    )


@pytest.mark.parametrize("status", ["open", "wontfix", "fixed", "declined"])
def test_all_statuses_valid(status: str) -> None:
    assert stamp_findings(BLOCK_A.replace("status: open", f"status: {status}"), "file").stamped == 1


def test_invalid_pass_name() -> None:
    with pytest.raises(ValueError, match="pass_name"):
        stamp_findings(BLOCK_A, "summary")


def test_stamp_file_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text(BLOCK_A + "\n", encoding="utf-8")
    assert stamp_findings_file(f, "file").stamped == 1
    mtime_text = f.read_text(encoding="utf-8")
    assert stamp_findings_file(f, "file").stamped == 0
    assert f.read_text(encoding="utf-8") == mtime_text


# ---- change context -------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, LocalReviewTarget]:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-qb", "feat/x")
    return tmp_path, LocalReviewTarget("feat/x", "main", base, base)


def _commit(path: Path, n: int, body: str = "") -> None:
    (path / "f.txt").write_text(f"{n}\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", f"commit {n}\n\n{body}" if body else f"commit {n}")


def test_description_small(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    _commit(path, 1, "body one")
    _commit(path, 2)
    desc = build_change_description(path, tgt)
    assert desc.startswith(f"## Change description\nBranch: feat/x\nBase ref: main\nMerge base: {tgt.base_sha}\n")
    assert desc.index("commit 1") < desc.index("commit 2")
    assert "body one" in desc
    assert "(truncated)" not in desc


def test_description_commit_cap(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    for i in range(1, 53):
        _commit(path, i)
    desc = build_change_description(path, tgt)
    assert desc.endswith("\n(truncated)")
    assert desc.count(" commit ") == 50
    assert len(desc) <= lr.MAX_DESCRIPTION_CHARS


def test_description_char_cap(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    for i in range(1, 6):
        _commit(path, i, "x" * 6000)
    desc = build_change_description(path, tgt)
    assert desc.endswith("\n(truncated)")
    assert len(desc) <= lr.MAX_DESCRIPTION_CHARS


def test_context_repo_name_and_fallback(repo: tuple[Path, LocalReviewTarget]) -> None:
    path, tgt = repo
    named = build_change_context("my-repo", path, tgt, "## Change description\nx")
    assert "Repo: my-repo\n" in named
    assert f"Commit: {tgt.head_sha}" in named
    assert "PR URL" not in named
    assert "PR number" not in named
    assert f"Repo: {path.name}\n" in build_change_context("", path, tgt, "d")


# ---- prompts / templates --------------------------------------------------


@pytest.mark.parametrize("name", list(lr.REVIEW_TEMPLATES))
def test_templates_are_local(name: str) -> None:
    text = (KNOWLEDGE / name).read_text(encoding="utf-8")
    assert lr.OUTPUT_FILE_PLACEHOLDER in text
    assert "dotharness-review-" not in text
    assert "gh api" not in text
    assert "gh pr comment" not in text


@pytest.mark.parametrize("name", [lr.FILE_TEMPLATE, lr.DESIGN_TEMPLATE])
def test_finding_templates_spec_format(name: str) -> None:
    text = (KNOWLEDGE / name).read_text(encoding="utf-8")
    assert "## Finding: <short title>\n- severity: P0 | P1\n- file: " in text
    assert "- status: open" in text


def test_prompts_assemble(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    file_t = (KNOWLEDGE / lr.FILE_TEMPLATE).read_text(encoding="utf-8")
    p = build_file_prompt(file_t, out, "\n\n## Diff for a\nD", "\n\nRepo: r", "GUIDE", "SONAR")
    assert str(out.resolve()) in p
    assert lr.OUTPUT_FILE_PLACEHOLDER not in p
    assert "## Additional Review Guide\nGUIDE" in p
    assert "## Static Analysis\nSONAR" in p
    s = build_summary_prompt("S {OUTPUT_FILE}", out, ["a.py", "b.py"], "\n\nRepo: r")
    assert s.endswith("Files reviewed:\na.py\nb.py")
    assert "## Additional Review Guide" not in s
    d = build_design_prompt("D {OUTPUT_FILE}", out, "\n\nDIFFS", "\n\nRepo: r")
    assert str(out.resolve()) in d
    assert "DIFFS" in d


# ---------------------------------------------------------------------------
# Review runner: output layout, manifest, idempotency, mutation guard
# ---------------------------------------------------------------------------

FINDING = "## Finding: Bug\n- severity: P1\n- file: a.py\n- line: 1\n- status: open\n\nBody."


class FakeBackend:
    """Stands in for harness.backend.Backend; `script(label, calls)` returns a CompletedProcess or raises."""

    def __init__(self, review_dir_fn, script=None) -> None:
        self.labels: list[str] = []
        self.review_dir_fn = review_dir_fn
        self.script = script

    def run(self, prompt, cwd, opencode_dir=None, context=None):
        label = context or ""
        self.labels.append(label)
        if self.script:
            res = self.script(label, cwd)
            if res is not None:
                return res
        out = self.review_dir_fn()
        if label.startswith("local-review file "):
            path = out / "files" / (label.removeprefix("local-review file ") + ".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FINDING + "\n", encoding="utf-8")
        elif label.endswith("summary"):
            (out / "summary.md").write_text("A summary.\n", encoding="utf-8")
        elif label.endswith("design"):
            (out / "design.md").write_text(FINDING + "\n\n## Summary\nok\n", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")


@pytest.fixture
def env(tmp_path: Path):
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("b")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature/x")
    (repo / "a.py").write_text("print(1)\n")
    (repo / "b.py").write_text("print(2)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat")
    kd = tmp_path / "knowledge"
    (kd / "pr-review").mkdir(parents=True)
    for name in lr.REVIEW_TEMPLATES:
        (kd / "pr-review" / name).write_text("T {OUTPUT_FILE}", encoding="utf-8")
    cfg = HarnessConfig(
        harness=HarnessSection(knowledge_dir=kd),
        repo=RepoConfig(name="o/r", working_dir=repo, name_provided=False),
    )
    target = lr.check_preconditions(repo, output_root=tmp_path / "out")
    root = tmp_path / "out"
    rdir = review_dir_for(root, cfg.repo_slug, target.branch, target.head_sha)
    return cfg, target, root, rdir, repo


def _m(d: Path) -> dict:
    m = read_manifest(d)
    assert m is not None
    return m


def _run(env, backend=None, **kw):
    cfg, target, root, rdir, _ = env
    backend = backend or FakeBackend(lambda: rdir)
    return backend, lr.run(cfg, target, output_root=root, backend=cast(Backend, backend), **kw)


def test_review_dir_keeps_slash_and_rejects_bad_components(tmp_path: Path) -> None:
    assert review_dir_for(tmp_path, "s", "feature/x", "abc") == tmp_path / "s" / "feature" / "x" / "abc"
    for bad in ("a//b", "..", "a/../b", "-x", "a/-b", ".", "a/", "/a"):
        with pytest.raises(LocalReviewError):
            review_dir_for(tmp_path, "s", bad, "abc")


def test_output_root_precedence(tmp_path: Path) -> None:
    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="o/r", working_dir=tmp_path),
        local_review=LocalReviewConfig(output_dir="~/cfgdir"),
    )
    assert resolve_output_root(cfg, "/x/y") == Path("/x/y")
    assert resolve_output_root(cfg) == Path("~/cfgdir").expanduser()
    cfg.local_review = LocalReviewConfig()
    assert resolve_output_root(cfg) == Path("~/.local/share/dotharness/reviews").expanduser()


def test_manifest_roundtrip_and_invalid_is_absent(tmp_path: Path) -> None:
    assert read_manifest(tmp_path) is None
    write_manifest(tmp_path, {"version": 1, "files": {"a": "done"}})
    assert read_manifest(tmp_path) == {"version": 1, "files": {"a": "done"}}
    assert not list(tmp_path.glob("*.tmp.*"))
    (tmp_path / "manifest.json").write_text("{not json")
    assert read_manifest(tmp_path) is None
    (tmp_path / "manifest.json").write_text(json.dumps({"version": 2, "files": {}}))
    assert read_manifest(tmp_path) is None


def test_manifest_write_is_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_manifest(tmp_path, {"version": 1, "files": {"a": "done"}})

    def boom(self, target):
        raise OSError("rename failed")  # noqa: TRY003

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="rename failed"):
        write_manifest(tmp_path, {"version": 1, "files": {"a": "failed"}})
    monkeypatch.undo()
    assert read_manifest(tmp_path) == {"version": 1, "files": {"a": "done"}}


def test_full_run_writes_layout_manifest_index_and_stamps(env) -> None:
    _cfg, target, root, rdir, _ = env
    backend, res = _run(env)
    assert res.ok
    assert backend.labels == [
        "local-review file a.py",
        "local-review file b.py",
        "local-review summary",
        "local-review design",
    ]
    m = json.loads((rdir / "manifest.json").read_text())
    assert m == {
        "version": 1,
        "branch": "feature/x",
        "base_ref": "main",
        "base_sha": target.base_sha,
        "head_sha": target.head_sha,
        "files": {"a.py": "done", "b.py": "done"},
        "summary": "done",
        "design": "done",
    }
    assert "- id: file-" in (rdir / "files" / "a.py.md").read_text()
    assert "- id: design-" in (rdir / "design.md").read_text()
    index = (rdir / "index.md").read_text()
    assert "files/a.py.md" in index
    assert target.head_sha in index
    assert rdir == root / "o-r" / "feature" / "x" / target.head_sha


def test_rerun_skips_done_and_reruns_only_failed(env) -> None:
    def script(label, cwd):
        if label == "local-review file b.py":
            return subprocess.CompletedProcess([], 1, "", "boom")
        return None

    _, first = _run(env, FakeBackend(lambda: env[3], script))
    assert first.failed == ["file:b.py", "summary"]
    m = _m(env[3])
    assert m["files"] == {"a.py": "done", "b.py": "failed"}
    assert m["summary"] == "failed"
    assert m["design"] == "done"

    backend, second = _run(env)
    assert second.ok
    assert backend.labels == ["local-review file b.py", "local-review summary"]

    backend, third = _run(env)
    assert backend.labels == []
    assert len(third.skipped) == 4


def test_timeout_marks_failed_and_continues(env) -> None:
    def script(label, cwd):
        if label == "local-review file a.py":
            raise subprocess.TimeoutExpired("x", 1)
        return None

    backend, res = _run(env, FakeBackend(lambda: env[3], script))
    assert res.failed == ["file:a.py", "summary"]
    assert "local-review design" in backend.labels


def test_missing_file_output_gets_no_findings_line_and_empty_summary_fails(env) -> None:
    def script(label, cwd):
        if label.startswith("local-review file ") or label.endswith("summary"):
            return subprocess.CompletedProcess([], 0, "", "")
        return None

    _, res = _run(env, FakeBackend(lambda: env[3], script))
    assert (env[3] / "files" / "a.py.md").read_text() == "No P0/P1 findings.\n"
    assert _m(env[3])["files"] == {"a.py": "done", "b.py": "done"}
    assert res.failed == ["summary"]


def test_force_reruns_everything_and_warns_about_address_statuses(env, caplog: pytest.LogCaptureFixture) -> None:
    _run(env)
    p = env[3] / "files" / "a.py.md"
    p.write_text(p.read_text().replace("status: open", "status: fixed"))
    with caplog.at_level(logging.WARNING):
        backend, res = _run(env, force=True)
    assert len(backend.labels) == 4
    assert "1 fixed, 0 declined" in caplog.text
    assert res.ok


def test_force_without_resolved_findings_does_not_warn(env, caplog: pytest.LogCaptureFixture) -> None:
    _run(env)
    with caplog.at_level(logging.WARNING):
        _run(env, force=True)
    assert "--force" not in caplog.text


def test_guard_head_moved_stops_and_writes_manifest(env) -> None:
    repo = env[4]

    def script(label, cwd):
        if label == "local-review file a.py":
            (repo / "c.py").write_text("x")
            _git(repo, "add", "c.py")
            _git(repo, "commit", "-m", "sneaky")
        return None

    backend = FakeBackend(lambda: env[3], script)
    with pytest.raises(RepoMutatedError, match="HEAD moved"):
        _run(env, backend)
    assert backend.labels == ["local-review file a.py"]
    m = _m(env[3])
    assert m["files"] == {"a.py": "failed"}
    assert (env[3] / "index.md").is_file()


def test_guard_tracked_change_stops_but_untracked_is_ignored(env) -> None:
    repo = env[4]

    def untracked(label, cwd):
        (repo / "scratch.txt").write_text("x")
        return None

    _, res = _run(env, FakeBackend(lambda: env[3], untracked))
    assert res.ok

    def tracked(label, cwd):
        (repo / "a.py").write_text("changed\n")
        return None

    backend = FakeBackend(lambda: env[3], tracked)
    with pytest.raises(RepoMutatedError, match=r"a\.py"):
        _run(env, backend, force=True)
    assert len(backend.labels) == 1
    assert (repo / "a.py").read_text() == "changed\n"  # nothing was restored


def test_no_writes_inside_repo(env) -> None:
    repo = env[4]
    _run(env)
    assert _git(repo, "status", "--porcelain") == ""


def test_output_dir_inside_repo_refused(env) -> None:
    cfg, target, _, _, repo = env
    with pytest.raises(LocalReviewError):
        lr.run(cfg, target, output_root=repo / "out", backend=cast(Backend, FakeBackend(lambda: repo)))
