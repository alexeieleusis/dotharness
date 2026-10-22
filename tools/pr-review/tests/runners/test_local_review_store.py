"""Focused tests for `local_review.store`: manifest, layout, reset, finding status and import isolation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness.config import HarnessConfig, HarnessSection, LocalReviewConfig, RepoConfig
from harness.runners.common import run_cmd
from harness.runners.local_review import store
from harness.runners.local_review.findings import stamp_findings
from harness.runners.local_review.store import (
    MANIFEST_VERSION,
    _count_resolved,
    _file_output_path,
    _new_manifest,
    _seed_manifest,
    _write_index,
    read_finding_status,
    read_manifest,
    reset,
    resolve_output_root,
    review_dir_for,
    review_root_for_reset,
    write_finding_status,
    write_manifest,
)
from harness.runners.local_review.types import LocalReviewError, LocalReviewTarget

PKG = "harness.runners.local_review"
TARGET = LocalReviewTarget(branch="feat/x", base_ref="main", base_sha="b" * 40, head_sha="a" * 40)

BLOCK = """## Finding: Off by one
- severity: P0
- file: src/a.py
- line: 12
- status: open

Body."""


def _cfg(tmp_path: Path, output_dir: str | None = None) -> HarnessConfig:
    return HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="o/r", working_dir=tmp_path),
        local_review=LocalReviewConfig(output_dir=output_dir),
    )


def _stamped(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "f.md"
    res = stamp_findings(BLOCK + "\n", "file")
    path.write_text(res.text, encoding="utf-8")
    fid = res.blocks[0].id
    assert fid
    return path, fid


def test_isolation_store_imports_without_backend_lock_or_orchestrators() -> None:
    pkg_dir = str(Path(store.__file__).parent)
    code = (
        f"import importlib, sys, types\n"
        f"pkg = types.ModuleType('{PKG}'); pkg.__path__ = [{pkg_dir!r}]; sys.modules['{PKG}'] = pkg\n"
        f"importlib.import_module('{PKG}.store')\n"
        f"bad = [m for m in ('harness.backend', 'harness.lock', '{PKG}.review', '{PKG}.address') if m in sys.modules]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)"
    )
    proc = run_cmd([sys.executable, "-c", code], cwd=os.getcwd(), env=dict(os.environ), timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_review_dir_layout_keeps_slash_in_branch(tmp_path: Path) -> None:
    assert review_dir_for(tmp_path, "o-r", "feat/x", "abc") == tmp_path / "o-r" / "feat" / "x" / "abc"


@pytest.mark.parametrize("branch", ["", "a//b", "..", "a/../b", "-x", "a/-b", "."])
def test_review_dir_rejects_unsafe_branch(tmp_path: Path, branch: str) -> None:
    with pytest.raises(LocalReviewError):
        review_dir_for(tmp_path, "o-r", branch, "abc")


def test_file_output_path_and_unsafe_paths(tmp_path: Path) -> None:
    assert _file_output_path(tmp_path, "src/a.py") == tmp_path / "files" / "src" / "a.py.md"
    for bad in ("../a.py", "src//a.py", "./a.py", ""):
        with pytest.raises(LocalReviewError):
            _file_output_path(tmp_path, bad)


def test_resolve_output_root_precedence(tmp_path: Path) -> None:
    assert resolve_output_root(_cfg(tmp_path, "~/cfg"), "/x/y") == Path("/x/y")
    assert resolve_output_root(_cfg(tmp_path, "~/cfg")) == Path("~/cfg").expanduser()
    assert resolve_output_root(_cfg(tmp_path)) == store.DEFAULT_OUTPUT_ROOT.expanduser()


def test_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = _new_manifest(TARGET)
    manifest["files"]["src/a.py"] = "done"
    write_manifest(tmp_path / "rd", manifest)
    assert read_manifest(tmp_path / "rd") == manifest
    assert manifest["version"] == MANIFEST_VERSION


def test_read_manifest_none_when_absent_corrupt_or_wrong_version(tmp_path: Path) -> None:
    assert read_manifest(tmp_path) is None
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert read_manifest(tmp_path) is None
    (tmp_path / "manifest.json").write_text('{"version": 99, "files": {}}', encoding="utf-8")
    assert read_manifest(tmp_path) is None
    (tmp_path / "manifest.json").write_text(f'{{"version": {MANIFEST_VERSION}, "files": []}}', encoding="utf-8")
    assert read_manifest(tmp_path) is None


def test_seed_manifest_carries_only_string_statuses() -> None:
    previous = {"files": {"a": "done", "b": {"x": 1}}, "summary": "failed", "design": 3}
    seeded = _seed_manifest(TARGET, previous)
    assert seeded["files"] == {"a": "done"}
    assert seeded["summary"] == "failed"
    assert "design" not in seeded
    assert _seed_manifest(TARGET, None)["files"] == {}


def test_write_index_lists_statuses(tmp_path: Path) -> None:
    manifest = _new_manifest(TARGET)
    manifest["files"]["src/a.py"] = "done"
    manifest["summary"] = "done"
    _write_index(tmp_path, manifest, ["src/a.py", "src/b.py"])
    text = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "# Local review: feat/x" in text
    assert "- [Summary](summary.md): done" in text
    assert "- [Design](design.md): not run" in text
    assert "- [src/a.py](files/src/a.py.md): done" in text
    assert "- [src/b.py](files/src/b.py.md): not run" in text


def test_review_root_for_reset_and_reset(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, str(tmp_path / "out"))
    root = review_root_for_reset(cfg)
    assert root == tmp_path / "out" / "o-r"
    (root / "x").mkdir(parents=True)
    reset(root)
    assert not root.exists()


def test_review_root_for_reset_refuses_non_child(tmp_path: Path) -> None:
    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="o/r", working_dir=tmp_path),
        local_review=LocalReviewConfig(output_dir=str(tmp_path / "out")),
    )
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (out / "o-r").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="Refusing"):
        review_root_for_reset(cfg)


def test_read_finding_status(tmp_path: Path) -> None:
    path, fid = _stamped(tmp_path)
    assert read_finding_status(path, fid) == "open"
    assert read_finding_status(path, "file-00000000") is None


def test_write_finding_status_fixed_adds_commit_and_changes_nothing_else(tmp_path: Path) -> None:
    path, fid = _stamped(tmp_path)
    before = path.read_text(encoding="utf-8")
    write_finding_status(path, fid, "fixed", commit="deadbeef")
    after = path.read_text(encoding="utf-8")
    assert after == before.replace("- status: open", "- status: fixed\n- commit: deadbeef")
    write_finding_status(path, fid, "fixed", commit="cafe")
    assert path.read_text(encoding="utf-8").count("- commit:") == 1
    assert "- commit: cafe" in path.read_text(encoding="utf-8")


def test_write_finding_status_declined_has_no_commit(tmp_path: Path) -> None:
    path, fid = _stamped(tmp_path)
    write_finding_status(path, fid, "declined", commit="ignored")
    text = path.read_text(encoding="utf-8")
    assert "- status: declined" in text
    assert "- commit:" not in text


def test_write_finding_status_errors(tmp_path: Path) -> None:
    path, fid = _stamped(tmp_path)
    with pytest.raises(ValueError, match="status"):
        write_finding_status(path, fid, "failed")
    with pytest.raises(LocalReviewError, match="not found"):
        write_finding_status(path, "file-00000000", "fixed")


def test_count_resolved(tmp_path: Path) -> None:
    path, fid = _stamped(tmp_path)
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "a.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    write_finding_status(tmp_path / "files" / "a.md", fid, "fixed", commit="c1")
    assert _count_resolved(tmp_path) == {"fixed": 1, "declined": 0}
