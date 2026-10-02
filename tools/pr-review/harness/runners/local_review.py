"""Git preconditions and base-ref resolution for `harness run local-review`.

Everything here is offline and read-only: no `gh`, no `git fetch`/`pull`/
`remote update`, nothing written inside the reviewed repo. Failures raise
`LocalReviewError` (caller prints the message to stderr and exits non-zero);
nothing-to-review raises the unrelated `NothingToReviewError` (caller prints
the message and exits 0), so the two cannot be confused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from harness.backend import Backend
from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.repo_guard import head_sha as read_head_sha
from harness.runners.common import (
    build_file_review_section,
    build_subprocess_env,
    get_changed_files,
    get_file_diff,
    get_vibe_heal_context,
    is_ancestor,
)

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 30
_GIT_TIMEOUT_RETURNCODE = 124  # same convention as timeout(1)
MAX_DIRTY_PATHS_SHOWN = 5
DEFAULT_BASE_FALLBACK = "main"
_NO_UNTRACKED_FLAG = "--untracked-files=no"


class LocalReviewError(RuntimeError):
    """A precondition failed; the command must exit non-zero."""


class NothingToReviewError(Exception):
    """The branch has no commits beyond the base; the command exits 0."""


@dataclass(frozen=True)
class LocalReviewTarget:
    branch: str
    base_ref: str
    base_sha: str  # the merge base
    head_sha: str


def _git(working_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    try:
        return subprocess.run(  # noqa: S603
            cmd,
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Surface a timeout as a failed command so callers' returncode handling covers it.
        return subprocess.CompletedProcess(
            cmd, _GIT_TIMEOUT_RETURNCODE, "", f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        )


def _ref_exists(working_dir: Path, ref: str) -> bool:
    return _git(working_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def resolve_base_ref(working_dir: Path, base: str | None = None, config_base: str | None = None) -> str:
    """First existing ref in order: `base` (--base), `config_base`, origin/HEAD target, `main`.

    An explicit `base` or `config_base` that does not resolve is an error and never
    falls through. The origin/HEAD and `main` rules fall through quietly.
    """
    for label, value in (("--base", base), ("[local_review].base", config_base)):
        if value:
            if _ref_exists(working_dir, value):
                return value
            raise LocalReviewError(f"base ref {value!r} (from {label}) does not resolve to a commit")  # noqa: TRY003

    tried: list[str] = []
    origin_head = _git(working_dir, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    target = origin_head.stdout.strip() if origin_head.returncode == 0 else ""
    if target:
        if _ref_exists(working_dir, target):
            return target
        tried.append(f"{target} (origin/HEAD)")
    else:
        tried.append("origin/HEAD (not set)")
    if _ref_exists(working_dir, DEFAULT_BASE_FALLBACK):
        return DEFAULT_BASE_FALLBACK
    tried.append(DEFAULT_BASE_FALLBACK)
    raise LocalReviewError(
        "could not resolve a base ref; tried: --base (not given), [local_review].base (not set), "
        + ", ".join(tried)
        + ". Pass --base REF."
    )


def compute_merge_base(working_dir: Path, base_ref: str) -> str:
    """`git merge-base <base_ref> HEAD`."""
    result = _git(working_dir, "merge-base", base_ref, "HEAD")
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise LocalReviewError(  # noqa: TRY003
            f"no merge base between {base_ref!r} and HEAD: {result.stderr.strip() or 'unrelated histories'}"
        )
    return sha


def check_output_dir_outside_repo(working_dir: Path, output_root: Path) -> None:
    """Refuse an output root equal to or inside `working_dir` (after ~ and symlink resolution)."""
    repo = working_dir.expanduser().resolve()
    out = output_root.expanduser().resolve()
    if out == repo or repo in out.parents:
        raise LocalReviewError(  # noqa: TRY003
            f"output directory {out} is inside the reviewed repo {repo}; choose a location outside it"
        )


def check_templates(knowledge_dir: Path, names: Sequence[str]) -> None:
    """Every `names` entry must exist under `knowledge_dir/pr-review/`."""
    for name in names:
        path = knowledge_dir / "pr-review" / name
        if not path.is_file():
            raise LocalReviewError(f"prompt template not found: {path}")  # noqa: TRY003


def check_preconditions(
    working_dir: Path,
    *,
    base: str | None = None,
    config_base: str | None = None,
    output_root: Path | None = None,
    review_phase: bool = True,
) -> LocalReviewTarget:
    """FR-2 items 1-5 plus the output-dir-inside-repo refusal. Template checks are separate.

    Raises LocalReviewError on failure, NothingToReviewError when there is nothing to review
    (only when `review_phase`).
    """
    working_dir = working_dir.expanduser().resolve()
    if output_root is not None:
        check_output_dir_outside_repo(working_dir, output_root)

    top = _git(working_dir, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise LocalReviewError(f"{working_dir} is not inside a git repository")  # noqa: TRY003
    if Path(top.stdout.strip()).resolve() != working_dir:
        raise LocalReviewError(  # noqa: TRY003
            f"repo.working_dir {working_dir} is not the toplevel of its git repo ({top.stdout.strip()})"
        )

    head_ref = _git(working_dir, "symbolic-ref", "--short", "HEAD")
    branch = head_ref.stdout.strip()
    if head_ref.returncode != 0 or not branch:
        raise LocalReviewError("HEAD is detached; check out a branch to review")  # noqa: TRY003

    status = _git(working_dir, "status", "--porcelain", _NO_UNTRACKED_FLAG)
    if status.returncode != 0:
        raise LocalReviewError(f"git status failed: {status.stderr.strip()}")  # noqa: TRY003
    dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        shown = ", ".join(dirty[:MAX_DIRTY_PATHS_SHOWN])
        more = f" (and {len(dirty) - MAX_DIRTY_PATHS_SHOWN} more)" if len(dirty) > MAX_DIRTY_PATHS_SHOWN else ""
        raise LocalReviewError(  # noqa: TRY003
            f"uncommitted changes to tracked files: {shown}{more}; uncommitted changes are not reviewed "
            "- commit or stash them first"
        )

    base_ref = resolve_base_ref(working_dir, base, config_base)
    head = _git(working_dir, "rev-parse", "HEAD")
    head_sha = head.stdout.strip()
    if head.returncode != 0 or not head_sha:
        raise LocalReviewError(f"cannot resolve HEAD: {head.stderr.strip()}")  # noqa: TRY003
    merge_base = compute_merge_base(working_dir, base_ref)

    if review_phase and (branch == base_ref or merge_base == head_sha):
        raise NothingToReviewError(f"Nothing to review: {branch} has no commits beyond {base_ref}")  # noqa: TRY003

    return LocalReviewTarget(branch=branch, base_ref=base_ref, base_sha=merge_base, head_sha=head_sha)


# ---------------------------------------------------------------------------
# Change context and prompt assembly (FR-4, FR-5)
# ---------------------------------------------------------------------------

MAX_DESCRIPTION_COMMITS = 50
MAX_DESCRIPTION_CHARS = 20_000
TRUNCATED_MARKER = "(truncated)"
OUTPUT_FILE_PLACEHOLDER = "{OUTPUT_FILE}"
FILE_TEMPLATE = "local-review-file.md"
SUMMARY_TEMPLATE = "local-review-summary.md"
DESIGN_TEMPLATE = "local-review-design.md"
REVIEW_TEMPLATES = (FILE_TEMPLATE, SUMMARY_TEMPLATE, DESIGN_TEMPLATE)


def build_change_description(working_dir: Path, target: LocalReviewTarget) -> str:
    """`## Change description`: branch, base ref, merge base, then the commit log.

    The log keeps the newest MAX_DESCRIPTION_COMMITS commits (listed oldest first) and the
    whole section is capped at MAX_DESCRIPTION_CHARS characters (cut at a line boundary).
    When either cap applies the text ends with a `(truncated)` line and stays within both caps.
    """
    header = (
        f"## Change description\nBranch: {target.branch}\nBase ref: {target.base_ref}\n"
        f"Merge base: {target.base_sha}\n\n"
    )
    count = _git(working_dir, "rev-list", "--count", f"{target.base_sha}..HEAD")
    total = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
    truncated = total > MAX_DESCRIPTION_COMMITS
    # --max-count keeps the newest N commits; --reverse then lists those oldest first.
    proc = _git(
        working_dir,
        "log",
        "--reverse",
        "--no-color",
        "--format=%h %s%n%b",
        f"--max-count={MAX_DESCRIPTION_COMMITS}",
        f"{target.base_sha}..HEAD",
    )
    log = proc.stdout.rstrip("\n") if proc.returncode == 0 else ""
    suffix = f"\n{TRUNCATED_MARKER}"
    budget = MAX_DESCRIPTION_CHARS - len(header) - len(suffix)
    if len(log) > budget:
        truncated = True
        cut = log[: max(budget, 0)]
        nl = cut.rfind("\n")
        log = cut[:nl] if nl > 0 else cut
    return header + log + (suffix if truncated else "")


def build_change_context(config_repo_name: str, working_dir: Path, target: LocalReviewTarget, description: str) -> str:
    """Replaces the PR URL / number / description block of the self-review prompts."""
    repo = config_repo_name or working_dir.name
    return f"\n\nRepo: {repo}\nCommit: {target.head_sha}\n\n{description}"


def _with_output_path(instructions: str, output_file: Path) -> str:
    return instructions.replace(OUTPUT_FILE_PLACEHOLDER, str(output_file.resolve()))


def _guide(extra_knowledge: str | None) -> str:
    return f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else ""


def _static(vibe_heal_context: str | None) -> str:
    return f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else ""


def build_file_prompt(
    instructions: str,
    output_file: Path,
    file_section: str,
    change_context: str,
    extra_knowledge: str | None = None,
    vibe_heal_context: str | None = None,
) -> str:
    """Per-file pass. `file_section` is `common.build_file_review_section(...)` output."""
    return (
        _with_output_path(instructions, output_file)
        + _guide(extra_knowledge)
        + file_section
        + change_context
        + _static(vibe_heal_context)
    )


def build_summary_prompt(
    instructions: str,
    output_file: Path,
    files: Sequence[str],
    change_context: str,
    extra_knowledge: str | None = None,
    vibe_heal_context: str | None = None,
) -> str:
    return (
        _with_output_path(instructions, output_file)
        + _guide(extra_knowledge)
        + change_context
        + "\n\nFiles reviewed:\n"
        + "\n".join(files)
        + _static(vibe_heal_context)
    )


def build_design_prompt(
    instructions: str,
    output_file: Path,
    diff_sections: str,
    change_context: str,
    extra_knowledge: str | None = None,
    vibe_heal_context: str | None = None,
) -> str:
    """Design pass. `diff_sections` is every changed file's `build_file_review_section` concatenated."""
    return (
        _with_output_path(instructions, output_file)
        + _guide(extra_knowledge)
        + diff_sections
        + change_context
        + _static(vibe_heal_context)
    )


# ---------------------------------------------------------------------------
# Finding blocks: parsing and id stamping
# ---------------------------------------------------------------------------

FINDING_HEADING = "## Finding:"
VALID_SEVERITIES = ("P0", "P1")
VALID_STATUSES = ("open", "wontfix", "fixed", "declined")
VALID_PASSES = ("file", "design")
_FIELD_RE = re.compile(r"^-\s*([A-Za-z_]+)\s*:\s*(.*?)\s*$")
_ID_RE = re.compile(r"(file|design)-[0-9a-f]{8}")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_H2_RE = re.compile(r"^## ")


@dataclass(frozen=True)
class FindingBlock:
    """One `## Finding:` block. `text` is the block exactly as written (no trailing newlines)."""

    title: str
    text: str
    start_line: int  # 0-based index of the heading line
    end_line: int  # exclusive
    fields: dict[str, str]
    severity: str | None = None
    file: str | None = None
    line: int | None = None
    status: str | None = None
    id: str | None = None
    valid: bool = False


@dataclass(frozen=True)
class StampResult:
    text: str  # the (possibly modified) full file content
    blocks: list[FindingBlock]  # parsed from `text` (after stamping)
    stamped: int  # ids newly added
    already_stamped: int  # valid blocks that already had an id (left unchanged)
    unparseable: int  # blocks missing/invalid a required field (left unstamped)


def sha8(block_text: str) -> str:
    return hashlib.sha256(block_text.encode("utf-8")).hexdigest()[:8]


def finding_id(pass_name: str, block_text: str) -> str:
    return f"{pass_name}-{sha8(block_text)}"


def _block_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) line ranges of finding blocks; a block ends at the next column-0 `## ` heading."""
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    in_fence = False
    for i, ln in enumerate(lines):
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence or not _H2_RE.match(ln):
            continue
        if start is not None:
            ranges.append((start, i))
            start = None
        if ln.startswith(FINDING_HEADING):
            start = i
    if start is not None:
        ranges.append((start, len(lines)))
    return ranges


def _metadata(lines: list[str]) -> dict[str, str]:
    """`- key: value` lines directly under the heading, up to the first blank line."""
    fields: dict[str, str] = {}
    for ln in lines[1:]:
        if not ln.strip():
            if fields:
                break
            continue
        m = _FIELD_RE.match(ln)
        if not m:
            break
        fields.setdefault(m.group(1).lower(), m.group(2))
    return fields


def _parse_block(lines: list[str], start: int, end: int) -> FindingBlock:
    seg = lines[start:end]
    while seg and not seg[-1].strip():
        seg.pop()
    text = "\n".join(seg)
    title = seg[0][len(FINDING_HEADING) :].strip()
    fields = _metadata(seg)
    severity = fields.get("severity")
    file = fields.get("file")
    status = fields.get("status")
    line_raw = fields.get("line", "")
    line = int(line_raw) if re.fullmatch(r"\d+", line_raw) else None
    raw_id = fields.get("id")
    id_ok = not raw_id or _ID_RE.fullmatch(raw_id) is not None  # ids become path components; reject others
    valid = severity in VALID_SEVERITIES and bool(file) and line is not None and status in VALID_STATUSES and id_ok
    return FindingBlock(
        title=title,
        text=text,
        start_line=start,
        end_line=start + len(seg),
        fields=fields,
        severity=severity,
        file=file,
        line=line,
        status=status,
        id=fields.get("id") or None,
        valid=valid,
    )


def parse_findings(text: str) -> list[FindingBlock]:
    """All finding blocks in `text`, valid or not (check `.valid`). Pure; logs nothing."""
    lines = text.split("\n")
    return [_parse_block(lines, s, e) for s, e in _block_ranges(lines)]


def stamp_findings(text: str, pass_name: str) -> StampResult:
    """Add `- id: <pass>-<8 hex>` (after the last metadata line) to every valid block lacking one.

    The hash is over the block exactly as written by the backend (heading through the last
    non-blank line, no trailing newline). Blocks that already carry an id keep it. Invalid
    blocks (missing or bad severity/file/line/status) are logged as warnings, left unstamped
    and counted in `unparseable`. Non-block content (e.g. a `## Summary` section) is untouched.
    """
    if pass_name not in VALID_PASSES:
        raise ValueError(f"pass_name must be one of {VALID_PASSES}, got {pass_name!r}")  # noqa: TRY003
    lines = text.split("\n")
    stamped = already = unparseable = 0
    inserts: list[tuple[int, str]] = []
    for s, e in _block_ranges(lines):
        blk = _parse_block(lines, s, e)
        if not blk.valid:
            unparseable += 1
            logger.warning("finding block %r (line %d) is missing a required field; left unstamped", blk.title, s + 1)
            continue
        if blk.id:
            already += 1
            continue
        # insertion point: just after the last metadata line (contiguous `- key:` lines)
        pos = s + 1
        while pos < blk.end_line and _FIELD_RE.match(lines[pos]):
            pos += 1
        inserts.append((pos, f"- id: {finding_id(pass_name, blk.text)}"))
        stamped += 1
    for pos, new in reversed(inserts):
        lines.insert(pos, new)
    new_text = "\n".join(lines)
    return StampResult(new_text, parse_findings(new_text), stamped, already, unparseable)


def stamp_findings_file(path: Path, pass_name: str) -> StampResult:
    """Read `path`, stamp it, and rewrite it only when something was stamped."""
    original = path.read_text(encoding="utf-8")
    result = stamp_findings(original, pass_name)
    if result.text != original:
        path.write_text(result.text, encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# Review runner: output layout, manifest, idempotency, mutation guard (FR-5/6/9)
# ---------------------------------------------------------------------------

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


class RepoMutatedError(LocalReviewError):
    """The backend changed HEAD or tracked files during the review phase (FR-9).

    Raised by `run` AFTER the manifest and index.md were written; the caller maps it
    to a non-zero exit. Nothing is reset, cleaned or restored.
    """


@dataclass
class ReviewResult:
    review_dir: Path
    manifest: dict
    ran: list[str] = field(default_factory=list)  # pass names that made a backend call
    skipped: list[str] = field(default_factory=list)  # pass names skipped as already done
    failed: list[str] = field(default_factory=list)  # pass names that failed

    @property
    def ok(self) -> bool:
        return not self.failed


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


def _guard_violation(working_dir: Path, expected_head: str) -> str | None:
    """Description of what the backend changed, or None. Untracked files are ignored (11.A-10)."""
    problems: list[str] = []
    try:
        now = read_head_sha(working_dir)
    except Exception as exc:
        now = ""
        problems.append(f"HEAD unreadable ({exc})")
    if now and now != expected_head:
        problems.append(f"HEAD moved from {expected_head} to {now}")
    status = _git(working_dir, "status", "--porcelain", _NO_UNTRACKED_FLAG)
    if status.returncode != 0:
        problems.append(f"git status failed: {status.stderr.strip()}")
    elif status.stdout.strip():
        changed = ", ".join(ln[3:] for ln in status.stdout.splitlines() if ln.strip())
        problems.append(f"tracked files changed: {changed}")
    return "; ".join(problems) or None


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


def _warn_discarded_statuses(review_dir: Path) -> None:
    counts = _count_resolved(review_dir)
    if counts["fixed"] or counts["declined"]:
        logger.warning(
            "--force discards address-phase statuses in %s: %d fixed, %d declined finding(s) will be overwritten",
            review_dir,
            counts["fixed"],
            counts["declined"],
        )


def _seed_manifest(target: LocalReviewTarget, previous: dict | None) -> dict:
    """A new manifest carrying over the string statuses of `previous` (when given)."""
    manifest = _new_manifest(target)
    if previous is not None:
        manifest["files"] = {k: v for k, v in previous["files"].items() if isinstance(v, str)}
        for key in ("summary", "design"):
            if isinstance(previous.get(key), str):
                manifest[key] = previous[key]
    return manifest


def _finish_file_output(out: Path) -> None:
    """Default a missing per-file output to "no findings", then stamp ids."""
    if not out.is_file():
        out.write_text(NO_FINDINGS_TEXT, encoding="utf-8")
    stamp_findings_file(out, "file")


def _single_pass_output_ok(name: str, out: Path, files_all_done: bool) -> bool:
    """Validate a finished summary/design pass: output present and non-empty; summary needs all files done."""
    if not out.is_file() or not out.read_text(encoding="utf-8").strip():
        logger.error("local-review %s: backend exited 0 but %s is missing or empty", name, out.name)
        return False
    if name == "summary" and not files_all_done:
        logger.warning("local-review summary: some file passes failed; summary recorded as failed")
        return False  # re-runs next time together with the failed files
    return True


class _ReviewPasses:
    """State and per-pass steps of one review-phase run (extracted from `run` to keep it flat)."""

    def __init__(
        self,
        config: HarnessConfig,
        target: LocalReviewTarget,
        review_dir: Path,
        manifest: dict,
        backend: Backend,
        env: dict[str, str],
        files: list[str],
        extra_knowledge: str | None,
    ) -> None:
        self.target = target
        self.review_dir = review_dir
        self.manifest = manifest
        self.backend = backend
        self.env = env
        self.files = files
        self.extra_knowledge = extra_knowledge
        self.wdir = Path(config.repo.working_dir)
        self.wdir_s = str(self.wdir)
        knowledge = Path(config.harness.knowledge_dir) / "pr-review"
        self.file_instr = (knowledge / FILE_TEMPLATE).read_text(encoding="utf-8")
        self.summary_instr = (knowledge / SUMMARY_TEMPLATE).read_text(encoding="utf-8")
        self.design_instr = (knowledge / DESIGN_TEMPLATE).read_text(encoding="utf-8")
        description = build_change_description(self.wdir, target)
        self.context = build_change_context(config.repo.name, self.wdir, target, description)
        self.vibe = get_vibe_heal_context(config.repo.subdirs, self.wdir_s, target.branch) or None
        self.result = ReviewResult(review_dir=review_dir, manifest=manifest)
        self.guard_head = read_head_sha(self.wdir)
        self.guard_errors: list[str] = []
        self.diff_cache: dict[str, str] = {}

    def section_for(self, f: str) -> str:
        if f not in self.diff_cache:
            self.diff_cache[f] = get_file_diff(
                f, self.target.base_ref, self.wdir_s, self.env, rev_range=f"{self.target.base_sha} HEAD"
            )
        return build_file_review_section(f, self.diff_cache[f], os.path.join(self.wdir_s, f))

    def call_backend(self, prompt: str, label: str) -> bool:
        """True when the backend exited 0 without timing out and the repo is unchanged."""
        ok = False
        try:
            proc = self.backend.run(prompt, cwd=self.wdir_s, context=label)
            ok = proc.returncode == 0
            if not ok:
                logger.error("%s: backend exited %d", label, proc.returncode)
        except subprocess.TimeoutExpired:
            logger.exception("%s: backend timed out", label)
        except Exception:
            logger.exception("%s: backend call failed", label)
        violation = _guard_violation(self.wdir, self.guard_head)
        if violation:
            msg = f"{label}: backend modified the repository: {violation}"
            self.guard_errors.append(msg)
            logger.error("%s", msg)
            return False
        return ok

    def record(self, name: str, ok: bool, store: Callable[[str], None]) -> None:
        store(STATUS_DONE if ok else STATUS_FAILED)
        write_manifest(self.review_dir, self.manifest)
        if not ok:
            self.result.failed.append(name)

    def file_pass(self, f: str) -> None:
        if self.manifest["files"].get(f) == STATUS_DONE:
            self.result.skipped.append(f"file:{f}")
            return
        out = _file_output_path(self.review_dir, f)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        section = self.section_for(f)
        prompt = build_file_prompt(self.file_instr, out, section, self.context, self.extra_knowledge, self.vibe)
        self.result.ran.append(f"file:{f}")
        ok = self.call_backend(prompt, f"local-review file {f}")
        if ok:
            _finish_file_output(out)
        self.record(f"file:{f}", ok, lambda st: self.manifest["files"].__setitem__(f, st))

    def run_file_passes(self) -> None:
        for f in self.files:
            if self.guard_errors:
                break
            self.file_pass(f)

    def single_pass(
        self, name: str, filename: str, build: Callable[[Path], str], stamp: str | None, files_all_done: bool
    ) -> None:
        if self.guard_errors:
            return
        if self.manifest.get(name) == STATUS_DONE and (name != "summary" or files_all_done):
            self.result.skipped.append(name)
            return
        out = self.review_dir / filename
        out.unlink(missing_ok=True)
        self.result.ran.append(name)
        ok = self.call_backend(build(out), f"local-review {name}")
        ok = ok and _single_pass_output_ok(name, out, files_all_done)
        if ok and stamp:
            stamp_findings_file(out, stamp)
        self.record(name, ok, lambda st: self.manifest.__setitem__(name, st))

    def run_summary_and_design(self) -> None:
        files_all_done = all(self.manifest["files"].get(f) == STATUS_DONE for f in self.files)
        self.single_pass(
            "summary",
            SUMMARY_NAME,
            lambda out: build_summary_prompt(
                self.summary_instr, out, self.files, self.context, self.extra_knowledge, self.vibe
            ),
            None,
            files_all_done,
        )
        self.single_pass(
            "design",
            DESIGN_NAME,
            lambda out: build_design_prompt(
                self.design_instr,
                out,
                "".join(self.section_for(f) for f in self.files),
                self.context,
                self.extra_knowledge,
                self.vibe,
            ),
            "design",
            files_all_done,
        )


def run(
    config: HarnessConfig,
    target: LocalReviewTarget,
    *,
    output_root: Path,
    force: bool = False,
    backend: Backend | None = None,
    extra_knowledge: str | None = None,
) -> ReviewResult:
    """Review phase after the lock is held and `check_preconditions` / `check_templates` passed.

    Writes only under `<output_root>/<repo_slug>/<branch>/<head_sha>/`. Passes: one per changed
    file not yet `done`, then summary, then design. The manifest is rewritten atomically after
    every pass. Returns a ReviewResult (`ok` False when any pass failed; caller maps to exit code).
    Raises RepoMutatedError (after writing manifest and index) when the backend moved HEAD or
    changed tracked files; LocalReviewError for an unusable branch name / output root.
    """
    wdir = Path(config.repo.working_dir)
    check_output_dir_outside_repo(wdir, output_root)
    review_dir = review_dir_for(output_root.expanduser(), config.repo_slug, target.branch, target.head_sha)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env)
    # keep user git config (color.diff=always) out of the diff text sent to the model
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "color.diff", "GIT_CONFIG_VALUE_0": "never"})
    if backend is None:
        backend = _make_backend(config)

    previous = None if force else read_manifest(review_dir)
    if force:
        _warn_discarded_statuses(review_dir)
    manifest = _seed_manifest(target, previous)

    files = get_changed_files(target.base_ref, str(wdir), env, rev_range=f"{target.base_sha}..HEAD")
    for f in files:
        _file_output_path(review_dir, f)  # validate before any backend call
    review_dir.mkdir(parents=True, exist_ok=True)
    manifest["files"] = {f: manifest["files"][f] for f in files if f in manifest["files"]}

    passes = _ReviewPasses(config, target, review_dir, manifest, backend, env, files, extra_knowledge)
    passes.run_file_passes()
    passes.run_summary_and_design()

    write_manifest(review_dir, manifest)
    _write_index(review_dir, manifest, files)
    if passes.guard_errors:
        raise RepoMutatedError("; ".join(passes.guard_errors))
    return passes.result


def _pass_lines(result: ReviewResult) -> list[str]:
    manifest = result.manifest
    statuses = list(manifest.get("files", {}).values())
    lines = [f"per-file: {sum(1 for st in statuses if st == STATUS_DONE)}/{len(statuses)} done"]
    for name in ("summary", "design"):
        lines.append(f"{name}: {manifest.get(name, 'not run')}")
    return lines


def run_local_review(
    config: HarnessConfig,
    base: str | None = None,
    output_dir: str | Path | None = None,
    force: bool = False,
    address: bool = False,
    skip_review: bool = False,
    review_dir: str | Path | None = None,
    *,
    backend: Backend | None = None,
) -> int:
    """CLI entry for `harness run local-review`; returns the process exit code.

    Takes the shared checkout lock first (same shape as `self_review.run`), then runs the
    locked body. The module-level `run` is the phase-05 review-phase runner and keeps its name,
    so this entry is `run_local_review`. `skip_review` and `review_dir` are only valid with
    `address` (and `review_dir` only with `skip_review`); violations are usage errors (11.A-19).
    """
    usage_error = address_usage_error(address, skip_review, review_dir)
    if usage_error:
        raise LocalReviewError(usage_error)
    with acquire_lock(config.lock_key):
        return _run_locked(
            config, base, output_dir, force, backend, address=address, skip_review=skip_review, review_dir=review_dir
        )


def _run_locked(
    config: HarnessConfig,
    base: str | None,
    output_dir: str | Path | None,
    force: bool,
    backend: Backend | None,
    *,
    address: bool = False,
    skip_review: bool = False,
    review_dir: str | Path | None = None,
) -> int:
    wdir = Path(config.repo.working_dir)
    review_ok = True
    try:
        output_root = resolve_output_root(config, output_dir)
        try:
            target = check_preconditions(
                wdir,
                base=base,
                config_base=config.local_review.base,
                output_root=output_root,
                review_phase=not skip_review,
            )
        except NothingToReviewError as exc:
            print(str(exc))
            return 0
        knowledge_dir = Path(config.harness.knowledge_dir)
        templates = ([] if skip_review else list(REVIEW_TEMPLATES)) + ([ADDRESS_TEMPLATE] if address else [])
        check_templates(knowledge_dir, templates)
        print(f"Base: {target.base_ref} (merge base {target.base_sha})")
        print(f"Head: {target.head_sha}")
        reviewed_dir: Path | None = None
        if not skip_review:
            extra = None
            kfile = config.harness.review_knowledge_file
            if kfile and kfile.exists():
                extra = kfile.read_text(encoding="utf-8")
            try:
                result = run(
                    config, target, output_root=output_root, force=force, backend=backend, extra_knowledge=extra
                )
            except RepoMutatedError as exc:
                rdir = review_dir_for(output_root, config.repo_slug, target.branch, target.head_sha)
                print(f"Review directory: {rdir}")
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(f"Review directory: {result.review_dir}")
            for line in _pass_lines(result):
                print(line)
            review_ok = result.ok
            reviewed_dir = result.review_dir
        if address:
            chosen = select_review_dir(
                config, target, output_root, review_dir=review_dir, skip_review=skip_review, reviewed_dir=reviewed_dir
            )
            print(f"Addressing review: {chosen}")
            collection = collect_findings(chosen)
            print(
                f"Findings to address: {len(collection.open)} open, "
                f"{collection.skipped_not_open} skipped (not open), {collection.unparseable} unparseable"
            )
            address_ok = run_address_phase(config, target, chosen, collection, backend=backend)
            return 0 if (review_ok and address_ok) else 1
    except LocalReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0 if review_ok else 1


# ---------------------------------------------------------------------------
# Address phase: selection (FR-10 step 1), collection (step 2), status helpers
# ---------------------------------------------------------------------------
ADDRESS_TEMPLATE = "local-address-finding.md"
STATUS_FIELD_RE = re.compile(r"^-\s*status\s*:", re.IGNORECASE)
COMMIT_FIELD_RE = re.compile(r"^-\s*commit\s*:", re.IGNORECASE)
RESOLUTIONS_DIR = "resolutions"


def address_usage_error(address: bool, skip_review: bool, review_dir: str | Path | None) -> str | None:
    if skip_review and not address:
        return "--skip-review requires --address"
    if review_dir and not address:
        return "--review-dir requires --address"
    if review_dir and not skip_review:
        return "--review-dir requires --skip-review"
    return None


@dataclass(frozen=True)
class AddressFinding:
    """A finding to address, with where it lives so statuses can be written back."""

    id: str
    pass_name: str  # "file" or "design"
    source_file: Path  # the review markdown file holding the block
    block: FindingBlock

    @property
    def file(self) -> str:
        return self.block.file or ""

    @property
    def line(self) -> int:
        return self.block.line or 1


@dataclass(frozen=True)
class FindingCollection:
    open: list[AddressFinding]  # ordered: per-file by path then line, then design in file order
    skipped_not_open: int
    unparseable: int


def _git_env() -> dict[str, str]:
    return build_subprocess_env([], {})


def _manifest_matches(manifest: dict | None, target: LocalReviewTarget, wdir: Path) -> bool:
    if manifest is None or manifest.get("branch") != target.branch:
        return False
    sha = manifest.get("head_sha")
    if not isinstance(sha, str) or not sha:
        return False
    if sha == target.head_sha:
        return True
    return is_ancestor(sha, target.head_sha, str(wdir), _git_env())


def _commit_time(wdir: Path, sha: str) -> int:
    proc = _git(wdir, "show", "-s", "--format=%ct", sha)
    try:
        return int(proc.stdout.strip()) if proc.returncode == 0 else -1
    except ValueError:
        return -1


def _check_explicit_review_dir(chosen: Path, target: LocalReviewTarget, wdir: Path) -> Path:
    manifest = read_manifest(chosen)
    if manifest is None:
        raise LocalReviewError(f"{chosen} does not contain a usable {MANIFEST_NAME}")  # noqa: TRY003
    if manifest.get("branch") != target.branch:
        raise LocalReviewError(  # noqa: TRY003
            f"review in {chosen} is for branch {manifest.get('branch')!r}, not {target.branch!r}"
        )
    if not _manifest_matches(manifest, target, wdir):
        raise LocalReviewError(  # noqa: TRY003
            f"review in {chosen} was made at {manifest.get('head_sha')}, which is not HEAD or an ancestor of HEAD"
        )
    return chosen


def _matching_review_candidates(
    branch_dir: Path, target: LocalReviewTarget, wdir: Path
) -> list[tuple[int, float, str, Path]]:
    """(commit time, manifest mtime, name, dir) for each review under `branch_dir` that matches `target`."""
    candidates: list[tuple[int, float, str, Path]] = []
    if not branch_dir.is_dir():
        return candidates
    for cand in sorted(branch_dir.iterdir()):
        manifest = read_manifest(cand) if cand.is_dir() else None
        if not _manifest_matches(manifest, target, wdir):
            continue
        sha = str((manifest or {}).get("head_sha"))
        try:
            mtime = (cand / MANIFEST_NAME).stat().st_mtime
        except OSError:
            continue
        candidates.append((_commit_time(wdir, sha), mtime, cand.name, cand))
    return candidates


def select_review_dir(
    config: HarnessConfig,
    target: LocalReviewTarget,
    output_root: Path,
    *,
    review_dir: str | Path | None = None,
    skip_review: bool = False,
    reviewed_dir: Path | None = None,
) -> Path:
    """FR-10 step 1 (7.2). Offline; raises LocalReviewError when no review directory qualifies."""
    wdir = Path(config.repo.working_dir).expanduser().resolve()
    if review_dir:
        return _check_explicit_review_dir(Path(review_dir).expanduser(), target, wdir)
    if not skip_review:
        chosen = reviewed_dir or review_dir_for(output_root, config.repo_slug, target.branch, target.head_sha)
        if read_manifest(chosen) is None:
            raise LocalReviewError(f"no completed review found in {chosen}")  # noqa: TRY003
        return chosen
    branch_dir = review_dir_for(output_root, config.repo_slug, target.branch, target.head_sha).parent
    candidates = _matching_review_candidates(branch_dir, target, wdir)
    if not candidates:
        raise LocalReviewError(  # noqa: TRY003
            f"no earlier review found for branch {target.branch!r} under {branch_dir}; run `harness run local-review` first"
        )
    return max(candidates)[3]


def _read_findings(path: Path, pass_name: str) -> tuple[list[AddressFinding], int, int]:
    """(open findings, skipped-not-open count, unparseable count) for one review markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("cannot read %s; skipping", path)
        return [], 0, 0
    found: list[AddressFinding] = []
    skipped = unparseable = 0
    for blk in parse_findings(text):
        if not blk.valid or not blk.id:
            logger.warning("ignoring unparseable or unstamped finding %r in %s", blk.title, path)
            unparseable += 1
        elif blk.status != "open":
            skipped += 1
        else:
            found.append(AddressFinding(blk.id, pass_name, path, blk))
    return found, skipped, unparseable


def collect_findings(review_dir: Path) -> FindingCollection:
    """FR-10 step 2 (7.3). Reads only `done` passes; returns open findings in deterministic order."""
    manifest = read_manifest(review_dir)
    if manifest is None:
        raise LocalReviewError(f"{review_dir} does not contain a usable {MANIFEST_NAME}")  # noqa: TRY003
    sources = [
        (_file_output_path(review_dir, changed), "file", True)
        for changed in sorted(path for path, st in manifest["files"].items() if st == STATUS_DONE)
    ]
    if manifest.get("design") == STATUS_DONE:
        sources.append((review_dir / DESIGN_NAME, "design", False))
    open_findings: list[AddressFinding] = []
    skipped = unparseable = 0
    for path, pass_name, by_line in sources:
        found, n_skipped, n_unparseable = _read_findings(path, pass_name)
        open_findings.extend(sorted(found, key=lambda f: f.line) if by_line else found)  # stable on ties
        skipped += n_skipped
        unparseable += n_unparseable
    return FindingCollection(open_findings, skipped, unparseable)


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


ADDRESS_HEADING = "**Finding to address**"
MAX_DIFF_CHARS = 40_000
OUTCOME_FIXED = "fixed"
OUTCOME_DECLINED = "declined"
OUTCOME_FAILED = "failed"


@dataclass
class AddressResult:
    """Per-finding outcomes of the address loop (finding id -> fixed|declined|failed)."""

    outcomes: dict[str, str] = field(default_factory=dict)
    not_reached: int = 0  # open findings never sent because the phase stopped early
    stop_reason: str | None = None

    def count(self, outcome: str) -> int:
        return sum(1 for v in self.outcomes.values() if v == outcome)

    @property
    def stopped(self) -> bool:
        return self.stop_reason is not None


def _current_branch(wdir: Path) -> str:
    proc = _git(wdir, "symbolic-ref", "--short", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _tracked_dirty(wdir: Path) -> list[str]:
    """Tracked paths with uncommitted changes (untracked files ignored, 11.A-10). Raises on git failure."""
    proc = _git(wdir, "status", "--porcelain", _NO_UNTRACKED_FLAG)
    if proc.returncode != 0:
        raise LocalReviewError(f"git status failed: {proc.stderr.strip()}")  # noqa: TRY003
    return [ln[3:] for ln in proc.stdout.splitlines() if ln.strip()]


def _head(wdir: Path) -> str:
    proc = _git(wdir, "rev-parse", "HEAD")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise LocalReviewError(f"cannot resolve HEAD: {proc.stderr.strip()}")  # noqa: TRY003
    return sha


def _state_problem(wdir: Path, branch: str) -> str | None:
    """Description of a wrong branch or a dirty tracked tree, or None (FR-2 items 1-3 re-check)."""
    try:
        current = _current_branch(wdir)
        if current != branch:
            return f"current branch is {current or '(detached HEAD)'!r}, expected {branch!r}"
        dirty = _tracked_dirty(wdir)
    except LocalReviewError as exc:
        return str(exc)
    if dirty:
        shown = ", ".join(dirty[:MAX_DIRTY_PATHS_SHOWN])
        more = f" (and {len(dirty) - MAX_DIRTY_PATHS_SHOWN} more)" if len(dirty) > MAX_DIRTY_PATHS_SHOWN else ""
        return f"uncommitted changes to tracked files: {shown}{more}"
    return None


def _resolution_decision(path: Path) -> str | None:
    """`fixed` / `declined` from the first line of the resolution file, else None."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.strip().splitlines()
    first = lines[0].strip().lower() if lines else ""
    for decision in (OUTCOME_FIXED, OUTCOME_DECLINED):
        if first == f"decision: {decision}":
            return decision
    return None


def _commit_parents(wdir: Path, rev: str) -> list[str]:
    proc = _git(wdir, "rev-list", "--parents", "-n", "1", rev)
    return proc.stdout.split()[1:] if proc.returncode == 0 else []


def build_address_prompt(
    instructions: str,
    finding: AddressFinding,
    resolution_path: Path,
    review_head_sha: str,
    diff: str,
    vibe_heal_context: str | None = None,
) -> str:
    """Template text, then the finding under `**Finding to address**` (the template has no placeholders)."""
    diff_text = diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + f"\n{TRUNCATED_MARKER}\n"
    parts = [
        instructions.rstrip(),
        "",
        "---",
        "",
        ADDRESS_HEADING,
        "",
        f"- finding id: {finding.id}",
        f"- review head sha (12 chars): {review_head_sha[:12]}",
        f"- file: {finding.file or '(none given)'}",
        f"- line: {finding.line}",
        f"- resolution file (absolute path, write it in Step 4): {resolution_path}",
        "",
        "Finding block:",
        "",
        finding.block.text.rstrip(),
        "",
        "Diff of the file against the merge base:",
        "",
        "```diff",
        diff_text.rstrip(),
        "```",
    ]
    if vibe_heal_context:
        parts += ["", "Static-analysis (vibe-heal) context:", "", vibe_heal_context.strip()]
    return "\n".join(parts) + "\n"


def _make_backend(config: HarnessConfig) -> Backend:
    return Backend(
        config.harness.backend,
        config.harness.backend_timeout_seconds,
        config.harness.path_prepend,
        dict(config.harness.env),
        expected_repo_name=config.repo.name if config.repo.name_provided else None,
    )


@dataclass(frozen=True)
class _AddressContext:
    target: LocalReviewTarget
    wdir: Path
    env: dict[str, str]
    backend: Backend
    instructions: str
    vibe: str | None
    review_head: str
    res_dir: Path

    @property
    def wdir_s(self) -> str:
        return str(self.wdir)


def _fail_finding(result: AddressResult, finding: AddressFinding, reason: str) -> None:
    result.outcomes[finding.id] = OUTCOME_FAILED
    logger.error("finding %s: failed (%s)", finding.id, reason)


def _stop_phase(result: AddressResult, finding: AddressFinding, reason: str) -> None:
    result.stop_reason = reason
    logger.error("address phase stopped: %s", reason)
    _fail_finding(result, finding, reason)


def _call_address_backend(ctx: _AddressContext, finding: AddressFinding, prompt: str) -> str:
    """Run the backend for one finding; return an error description, or "" when it exited 0."""
    try:
        proc = ctx.backend.run(prompt, cwd=ctx.wdir_s, context=f"local-review address {finding.id}")
    except subprocess.TimeoutExpired:
        return "backend timed out"
    except Exception as exc:
        logger.exception("finding %s: backend call failed", finding.id)
        return f"backend call failed ({exc})"
    return "" if proc.returncode == 0 else f"backend exited {proc.returncode}"


def _post_call_stop_reasons(ctx: _AddressContext, pre_sha: str) -> tuple[str, list[str]]:
    """7.4 checks after a backend call; return (new HEAD or "", reasons to stop the phase)."""
    reasons: list[str] = []
    try:
        new_head = _head(ctx.wdir)
        if not is_ancestor(pre_sha, new_head, ctx.wdir_s, ctx.env):
            reasons.append(f"history rewritten: {pre_sha} is not an ancestor of HEAD {new_head}")
        now_branch = _current_branch(ctx.wdir)
        if now_branch != ctx.target.branch:
            reasons.append(f"branch changed from {ctx.target.branch!r} to {now_branch or '(detached HEAD)'!r}")
        dirty = _tracked_dirty(ctx.wdir)
        if dirty:
            reasons.append(f"tracked files have uncommitted changes: {', '.join(dirty[:MAX_DIRTY_PATHS_SHOWN])}")
    except LocalReviewError as exc:
        return "", [str(exc)]
    return new_head, reasons


def _verify_resolution(
    ctx: _AddressContext, resolution: Path, pre_sha: str, new_head: str
) -> tuple[str, str | None, str]:
    """7.3: cross-check the resolution file against git; return (decision, commit, error)."""
    decision = _resolution_decision(resolution)
    if decision is None:
        return "", None, f"resolution file {resolution} is missing, empty or its first line is not 'decision: ...'"
    if decision == OUTCOME_FIXED:
        if new_head == pre_sha or _commit_parents(ctx.wdir, new_head) != [pre_sha]:
            return decision, None, f"decision is fixed but HEAD is not exactly one new commit on {pre_sha[:12]}"
        return decision, new_head, ""
    if new_head != pre_sha:
        return decision, None, "decision is declined but HEAD moved"
    return decision, None, ""


def _address_one(ctx: _AddressContext, finding: AddressFinding, result: AddressResult) -> bool:
    """Address one finding and record its outcome; return False when the phase must stop."""
    problem = _state_problem(ctx.wdir, ctx.target.branch)
    if problem:
        _stop_phase(result, finding, f"before finding {finding.id}: {problem}")
        return False
    try:
        pre_sha = _head(ctx.wdir)
    except LocalReviewError as exc:
        _stop_phase(result, finding, str(exc))
        return False
    resolution = ctx.res_dir / f"{finding.id}.md"
    if resolution.resolve().parent != ctx.res_dir.resolve():
        _fail_finding(result, finding, "finding id resolves outside the resolutions directory")
        return True
    resolution.unlink(missing_ok=True)  # a stale file must not be mistaken for this run's answer
    diff = ""
    if finding.file:
        diff = get_file_diff(
            finding.file, ctx.target.base_ref, ctx.wdir_s, ctx.env, rev_range=f"{ctx.target.base_sha} HEAD"
        )
    prompt = build_address_prompt(ctx.instructions, finding, resolution.resolve(), ctx.review_head, diff, ctx.vibe)
    backend_error = _call_address_backend(ctx, finding, prompt)

    # 7.4: stop-the-phase checks, after every call, even a failed one. No repair (G3).
    new_head, stop_reasons = _post_call_stop_reasons(ctx, pre_sha)
    if stop_reasons:
        _stop_phase(result, finding, f"after finding {finding.id}: " + "; ".join(stop_reasons))
        return False
    if backend_error:
        _fail_finding(result, finding, backend_error)
        return True

    decision, commit, error = _verify_resolution(ctx, resolution, pre_sha, new_head)
    if error:
        _fail_finding(result, finding, error)
        return True
    try:
        write_finding_status(finding.source_file, finding.id, decision, commit)
    except (LocalReviewError, OSError) as exc:
        _fail_finding(result, finding, f"cannot record status: {exc}")
        return True
    outcome = OUTCOME_FIXED if decision == OUTCOME_FIXED else OUTCOME_DECLINED
    result.outcomes[finding.id] = outcome
    logger.info("finding %s: %s%s", finding.id, outcome, f" ({commit})" if commit else "")
    return True


def address_findings(
    config: HarnessConfig,
    target: LocalReviewTarget,
    review_dir: Path,
    collection: FindingCollection,
    *,
    backend: Backend | None = None,
) -> AddressResult:
    """FR-10 steps 3-4: one backend call and at most one commit per open finding, verified by git.

    A finding becomes `fixed` / `declined` only when the resolution file and git agree (7.3);
    otherwise it is `failed` and keeps `status: open`. Stops (no repair, no further backend calls)
    when history was rewritten, tracked files are dirty, or the branch changed (7.4).
    """
    wdir = Path(config.repo.working_dir).expanduser().resolve()
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env)
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "color.diff", "GIT_CONFIG_VALUE_0": "never"})
    review_head = target.head_sha
    manifest = read_manifest(review_dir)
    if manifest and isinstance(manifest.get("head_sha"), str):
        review_head = manifest["head_sha"]
    ctx = _AddressContext(
        target=target,
        wdir=wdir,
        env=env,
        backend=backend or _make_backend(config),
        instructions=(Path(config.harness.knowledge_dir) / "pr-review" / ADDRESS_TEMPLATE).read_text(encoding="utf-8"),
        vibe=get_vibe_heal_context(config.repo.subdirs, str(wdir), target.branch) or None,
        review_head=review_head,
        res_dir=review_dir / RESOLUTIONS_DIR,
    )
    result = AddressResult()
    pending = list(collection.open)
    ctx.res_dir.mkdir(parents=True, exist_ok=True)
    for index, finding in enumerate(pending):
        if not _address_one(ctx, finding, result):
            result.not_reached = len(pending) - index - 1
            break
    return result


def address_summary_line(collection: FindingCollection, result: AddressResult) -> str:
    skipped = collection.skipped_not_open + result.not_reached
    return (
        f"findings: {result.count(OUTCOME_FIXED)} fixed, {result.count(OUTCOME_DECLINED)} declined, "
        f"{result.count(OUTCOME_FAILED)} failed, {skipped} skipped (not open), {collection.unparseable} unparseable"
    )


def run_address_phase(
    config: HarnessConfig,
    target: LocalReviewTarget,
    review_dir: Path,
    collection: FindingCollection,
    *,
    backend: Backend | None = None,
) -> bool:
    """Run the address loop, print the FR-9 summary line, return True when the exit code should be 0.

    False when any finding failed, any block was unparseable, or the phase stopped early.
    Not-reached findings after an early stop count as skipped, not failed.
    """
    result = address_findings(config, target, review_dir, collection, backend=backend)
    print(address_summary_line(collection, result))
    if result.stopped:
        print(f"ERROR: address phase stopped early: {result.stop_reason}", file=sys.stderr)
    return not (result.stopped or result.count(OUTCOME_FAILED) or collection.unparseable)
