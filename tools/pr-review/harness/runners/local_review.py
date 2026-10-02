"""Git preconditions and base-ref resolution for `harness run local-review`.

Everything here is offline and read-only: no `gh`, no `git fetch`/`pull`/
`remote update`, nothing written inside the reviewed repo. Failures raise
`LocalReviewError` (caller prints the message to stderr and exits non-zero);
nothing-to-review raises the unrelated `NothingToReviewError` (caller prints
the message and exits 0), so the two cannot be confused.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 30
MAX_DIRTY_PATHS_SHOWN = 5
DEFAULT_BASE_FALLBACK = "main"


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
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(working_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
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

    status = _git(working_dir, "status", "--porcelain", "--untracked-files=no")
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
    valid = severity in VALID_SEVERITIES and bool(file) and line is not None and status in VALID_STATUSES
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
