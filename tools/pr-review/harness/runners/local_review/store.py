"""Review output layout: manifest, index, finding-status helpers and reset.

Imports only `types`, `findings` and `config` (type hints); never the backend, lock or orchestrators.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from harness.runners.local_review.findings import (
    _FIELD_RE,
    COMMIT_FIELD_RE,
    STATUS_FIELD_RE,
    VALID_STATUSES,
    parse_findings,
)
from harness.runners.local_review.types import LocalReviewError, LocalReviewTarget

if TYPE_CHECKING:
    from harness.config import HarnessConfig

DEFAULT_OUTPUT_ROOT = Path("~/.local/share/dotharness/reviews")
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
INDEX_NAME = "index.md"
SUMMARY_NAME = "summary.md"
DESIGN_NAME = "design.md"
FILES_DIR = "files"
NO_FINDINGS_TEXT = "No P0/P1 findings.\n"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
RESOLUTIONS_DIR = "resolutions"


def review_root_for_reset(config: HarnessConfig) -> Path:
    """The per-repo review directory `state reset local-review` deletes; refuses anything
    that is not a direct child of the output root."""
    output_root = resolve_output_root(config)
    review_root = output_root / config.repo_slug
    if review_root.resolve().parent != output_root.resolve():
        raise ValueError(f"Refusing to delete {review_root}: not a direct child of {output_root}")  # noqa: TRY003
    return review_root


def reset(review_root: Path) -> None:
    shutil.rmtree(review_root)


def resolve_output_root(config: HarnessConfig, output_dir: str | Path | None = None) -> Path:
    """`--output-dir`, else `[local_review].output_dir`, else the default; `~` expanded."""
    raw = output_dir or config.local_review.output_dir or DEFAULT_OUTPUT_ROOT
    return Path(raw).expanduser()


def review_dir_for(output_root: Path, repo_slug: str, branch: str, head_sha: str) -> Path:
    """`<output_root>/<repo_slug>/<branch>/<head_sha>/`; `/` in branch stays a separator.

    A branch component that is empty, `.`, `..` or starts with `-` is rejected (11.A-2).
    """
    for part in branch.split("/"):
        if part in ("", ".", "..") or part.startswith("-"):
            raise LocalReviewError(f"branch name {branch!r} cannot be used as an output directory")  # noqa: TRY003
    return output_root.joinpath(repo_slug, *branch.split("/"), head_sha)


def _file_output_path(review_dir: Path, changed_file: str) -> Path:
    parts = changed_file.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise LocalReviewError(f"unsafe changed file path {changed_file!r}")  # noqa: TRY003
    return review_dir / FILES_DIR / (changed_file + ".md")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_manifest(review_dir: Path) -> dict | None:
    """The manifest, or None when absent, corrupt or of another `version` (all passes then run)."""
    path = review_dir / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION or not isinstance(data.get("files"), dict):
        return None
    return data


def write_manifest(review_dir: Path, manifest: dict) -> None:
    _atomic_write_text(review_dir / MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")


def _new_manifest(target: LocalReviewTarget) -> dict:
    return {
        "version": MANIFEST_VERSION,
        "branch": target.branch,
        "base_ref": target.base_ref,
        "base_sha": target.base_sha,
        "head_sha": target.head_sha,
        "files": {},
    }


def _write_index(review_dir: Path, manifest: dict, files: Sequence[str]) -> None:
    def status(value: str | None) -> str:
        return value or "not run"

    lines = [
        f"# Local review: {manifest['branch']}",
        "",
        f"- Base ref: {manifest['base_ref']} (merge base {manifest['base_sha']})",
        f"- Head: {manifest['head_sha']}",
        "",
        "## Passes",
        "",
        f"- [Summary]({SUMMARY_NAME}): {status(manifest.get('summary'))}",
        f"- [Design]({DESIGN_NAME}): {status(manifest.get('design'))}",
        "",
        "## Files",
        "",
    ]
    lines.extend(f"- [{f}]({FILES_DIR}/{f}.md): {status(manifest['files'].get(f))}" for f in files)
    _atomic_write_text(review_dir / INDEX_NAME, "\n".join(lines) + "\n")


def _count_resolved(review_dir: Path) -> dict[str, int]:
    counts = {"fixed": 0, "declined": 0}
    paths = [*sorted((review_dir / FILES_DIR).rglob("*.md")), review_dir / DESIGN_NAME]
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for blk in parse_findings(text):
            if blk.status in counts:
                counts[blk.status] += 1
    return counts


def _seed_manifest(target: LocalReviewTarget, previous: dict | None) -> dict:
    """A new manifest carrying over the string statuses of `previous` (when given)."""
    manifest = _new_manifest(target)
    if previous is not None:
        manifest["files"] = {k: v for k, v in previous["files"].items() if isinstance(v, str)}
        for key in ("summary", "design"):
            if isinstance(previous.get(key), str):
                manifest[key] = previous[key]
    return manifest


def read_finding_status(source_file: Path, finding_id: str) -> str | None:
    """Current `status` of the block with `finding_id` in `source_file`, or None if not found."""
    for blk in parse_findings(source_file.read_text(encoding="utf-8")):
        if blk.id == finding_id:
            return blk.status
    return None


def write_finding_status(source_file: Path, finding_id: str, status: str, commit: str | None = None) -> None:
    """Set the `status` line of one block; for `fixed` with `commit`, also add `- commit: <sha>`.

    Nothing else in the file changes. A `failed` attempt is not a status: callers leave `open` alone.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")  # noqa: TRY003
    text = source_file.read_text(encoding="utf-8")
    lines = text.split("\n")
    blk = next((b for b in parse_findings(text) if b.id == finding_id), None)
    if blk is None:
        raise LocalReviewError(f"finding {finding_id} not found in {source_file}")  # noqa: TRY003
    pos = blk.start_line + 1
    status_idx = None
    commit_idx = None
    while pos < blk.end_line and not lines[pos].strip():
        pos += 1
    while pos < blk.end_line and _FIELD_RE.match(lines[pos]):
        if STATUS_FIELD_RE.match(lines[pos]):
            status_idx = pos
        elif COMMIT_FIELD_RE.match(lines[pos]):
            commit_idx = pos
        pos += 1
    if status_idx is None:
        raise LocalReviewError(f"finding {finding_id} has no status line in {source_file}")  # noqa: TRY003
    lines[status_idx] = f"- status: {status}"
    if status == "fixed" and commit:
        if commit_idx is not None:
            lines[commit_idx] = f"- commit: {commit}"
        else:
            lines.insert(status_idx + 1, f"- commit: {commit}")
    _atomic_write_text(source_file, "\n".join(lines))
