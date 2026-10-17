"""Intake stage: turn a work-item reference into a grounded brief via an injected agent runner.

The runner is injected (``Callable[[str], None]`` taking the prompt text), so no agent is ever
launched here. The stage writes progress to the run record before and after, refuses to run when
the target repository's ``git status`` changes, and reports whether the brief still has blocking
open decisions (checkpoint C1). Imports only Package A modules (never ``harness`` or ``spec_prism_flow``).
"""

# ruff: noqa: TRY003
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sisyphus.config import SisyphusConfig
from sisyphus.paths import knowledge_file
from sisyphus.rails import Checkpoint
from sisyphus.runrecord import RunRecord, encode_item
from sisyphus.stages import INTAKE_STAGE

PROMPT_FILE = "sisyphus/intake-prompt.md"
DRAFTING_PROMPT_FILE = "spec-prism-flow/requirements-doc-drafting-prompt.md"
ITEM_PLACEHOLDER = "<work-item reference>"
PATH_PLACEHOLDER = "<brief path>"
SIDECAR_SUFFIX = ".intake.json"

AgentRunner = Callable[[str], None]


class IntakeError(Exception):
    """Raised for an unusable config, a missing brief after the runner, or a git failure."""


class BoundaryViolation(IntakeError):
    """Raised when the target repository's git status changed during the stage (R-2)."""


@dataclass(frozen=True)
class OpenDecision:
    number: int
    text: str


@dataclass(frozen=True)
class IntakeResult:
    brief_path: Path
    skipped: bool
    open_decisions: tuple[OpenDecision, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.open_decisions)

    @property
    def checkpoint(self) -> Checkpoint | None:
        """``Checkpoint.C1`` when blocking open decisions exist, else ``None``."""
        return Checkpoint.C1 if self.open_decisions else None


# ------------------------------------------------------------------ prompt


def build_prompt(template: str, item: str, brief_path: Path | str, drafting_prompt_path: Path | str) -> str:
    """Substitute the two placeholders and append the resolved drafting-prompt path as a trailer."""
    text = template.replace(ITEM_PLACEHOLDER, item).replace(PATH_PLACEHOLDER, str(brief_path))
    return f"{text.rstrip()}\n\nrequirements-doc-drafting-prompt.md is at {drafting_prompt_path}\n"


def resolve_brief_path(config: SisyphusConfig, item: str) -> Path:
    """Expand ``[project].brief_path_template`` (placeholders ``<workspace_dir>`` and ``<item-id>``)."""
    template = config.project.brief_path_template
    if not template:
        raise IntakeError("[project].brief_path_template is not set")
    path = template.replace("<item-id>", encode_item(item))
    if "<workspace_dir>" in path:
        if not config.project.workspace_dir:
            raise IntakeError("brief_path_template uses <workspace_dir> but [project].workspace_dir is not set")
        path = path.replace("<workspace_dir>", config.project.workspace_dir)
    return Path(path).expanduser()


# ------------------------------------------------------- open-decisions log

_HEADING = re.compile(
    r"^(?:(#{1,6})\s*(?:\d+\.\s*)?|\*\*\s*)open[- ](?:decisions?|questions?)\b.*$",
    re.IGNORECASE,
)
_ANY_HEADING = re.compile(r"^(#{1,6})\s")
_ENTRY = re.compile(r"^\s*(?:(\d+)[.)]|[-*+]\s+D-(\d+)\s*[:.)]?)\s+(.*)$")
_RESOLVED = re.compile(r"(?:^\W*resolved\b)|(?:\bstatus:\s*resolved\b)", re.IGNORECASE)
_NONE = re.compile(r"^\W*(?:none|n/?a|nothing|no (?:open )?(?:decisions?|questions?))\b\W*$", re.IGNORECASE)
_MENTION = re.compile(r"open[- ]decisions?", re.IGNORECASE)


def _unparsed(reason: str) -> tuple[OpenDecision, ...]:
    return (OpenDecision(0, f"Open-decisions log could not be parsed ({reason}); resolve it by hand"),)


def parse_open_decisions(text: str) -> tuple[OpenDecision, ...]:
    """Return the unresolved entries of the brief's open-decisions log, failing closed.

    The log is the section under a heading (or a bold line) named "Open decisions"; entries are
    numbered items or ``- D-n:`` bullets. An entry starting with "resolved" (after any markup) or
    containing "status: resolved" is not blocking. A section that is explicitly "None" yields no
    decisions. If the text mentions open decisions but no section parses, or a section has content
    but no entries, a single sentinel decision (number 0) is returned so C1 still blocks.
    """
    entries: list[list[str]] = []
    level = 0
    found = False
    body: list[str] = []
    for line in text.splitlines():
        if not found:
            m = _HEADING.match(line)
            if m:
                found = True
                level = len(m.group(1)) if m.group(1) else 7
            continue
        heading = _ANY_HEADING.match(line)
        if heading and len(heading.group(1)) <= level:
            break
        if line.strip():
            body.append(line.strip())
        entry = _ENTRY.match(line)
        if entry:
            entries.append([entry.group(1) or entry.group(2), entry.group(3).strip()])
        elif entries and line.strip():
            entries[-1][1] += " " + line.strip()
    if not found:
        return _unparsed("no recognisable section") if _MENTION.search(text) else ()
    if not entries and body and not all(_NONE.match(b) for b in body):
        return _unparsed("section has content but no entries")
    return tuple(OpenDecision(int(n), t) for n, t in entries if not _RESOLVED.search(t))


# ----------------------------------------------------------- git boundary


def _git(repo_dir: Path, *args: str, ok_codes: tuple[int, ...] = (0,)) -> bytes:
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_dir), *args],  # noqa: S607
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise IntakeError(f"git {' '.join(args)} failed in {repo_dir}: {exc}") from exc
    if proc.returncode not in ok_codes:
        raise IntakeError(f"git {' '.join(args)} failed in {repo_dir}: exit status {proc.returncode}")
    return proc.stdout


def snapshot_git_status(
    repo_dir: Path | str, ignore: Iterable[Path | str] = ()
) -> tuple[bytes, dict[str, str], bytes, bytes]:
    """Snapshot ``git status`` (ignored files included), a content hash of every non-clean file, HEAD and the branch.

    Hashes make a second edit to an already-modified file visible; HEAD and the symbolic ref expose
    commits, resets and branch switches that leave the status unchanged. ``ignore`` lists paths
    (absolute, or relative to ``repo_dir``) to leave out, such as a brief that lives inside the repository.
    """
    repo = Path(repo_dir)
    root = repo.resolve()
    skip = {(p if Path(p).is_absolute() else root / p) for p in map(Path, ignore)}
    skip = {Path(p).resolve() for p in skip}
    raw = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
    kept: list[bytes] = []
    hashes: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if len(entry) <= 3:
            continue
        name = entry[3:].decode("utf-8", "surrogateescape")
        if (root / name).resolve() in skip:
            continue
        kept.append(entry)
        target = repo / name
        if target.is_file():
            hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", ok_codes=(0, 1))  # exit 1: no commits yet
    branch = _git(repo, "symbolic-ref", "-q", "HEAD", ok_codes=(0, 1))  # exit 1: detached HEAD
    return b"\0".join(kept), hashes, head, branch


@contextmanager
def repo_unchanged(repo_dir: Path | str, ignore: Iterable[Path | str] = ()) -> Generator[None]:
    """Fail with :class:`BoundaryViolation` if the git state of ``repo_dir`` differs after the body, even when it raises."""
    ignore = tuple(ignore)
    before = snapshot_git_status(repo_dir, ignore)
    message = f"intake changed the target repository {repo_dir}: git state differs before and after"
    try:
        yield
    except BaseException as exc:
        # A failing runner may still have modified the repository: report that instead of the bare error.
        if snapshot_git_status(repo_dir, ignore) != before:
            raise BoundaryViolation(message) from exc
        raise
    if snapshot_git_status(repo_dir, ignore) != before:
        raise BoundaryViolation(message)


# ------------------------------------------------------------------ stage


def _sidecar(brief: Path) -> Path:
    return brief.with_name(brief.name + SIDECAR_SUFFIX)


def _brief_stamp(brief: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of the brief, or ``None`` when absent; detects a stale brief the runner left alone."""
    try:
        st = brief.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _unchanged(brief: Path, item: str, digest: str) -> bool:
    if not brief.is_file():
        return False
    try:
        data = json.loads(_sidecar(brief).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("item") == item and data.get("prompt_sha256") == digest


def run_intake(
    item: str,
    config: SisyphusConfig,
    runner: AgentRunner,
    record: RunRecord | None = None,
    home: Path | None = None,
    checkout_root: Path | None = None,
    reparse_only: bool = False,
) -> IntakeResult:
    """Run the intake stage for ``item`` and report open decisions.

    Skips ``runner`` when the brief exists and the item reference plus the SHA-256 of the final
    prompt text match the sidecar written next to the brief on the last successful run. The
    ``reparse_only`` never calls the runner when the brief exists (a resume after C1 must not redraft a
    human-edited brief); it only re-parses the open decisions. The
    stage is marked in progress before and complete after in the run record; a runner exception or
    boundary violation leaves it in progress. A blocking brief sets checkpoint C1 on the record.
    """
    if not config.project.repo_dir:
        raise IntakeError("[project].repo_dir is not set")
    if not config.project.workspace_dir:
        raise IntakeError("[project].workspace_dir is not set")
    repo = Path(config.project.repo_dir).expanduser()
    brief = resolve_brief_path(config, item)
    template = knowledge_file(PROMPT_FILE, home, checkout_root).read_text(encoding="utf-8")
    prompt = build_prompt(template, item, brief, knowledge_file(DRAFTING_PROMPT_FILE, home, checkout_root))
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    rec = record or RunRecord.open(Path(config.project.workspace_dir).expanduser(), item)

    skipped = (reparse_only and brief.is_file()) or _unchanged(brief, item, digest)
    with rec.stage_scope(INTAKE_STAGE):
        if not skipped:
            brief.parent.mkdir(parents=True, exist_ok=True)
            before = _brief_stamp(brief)
            with repo_unchanged(repo, ignore=(brief, _sidecar(brief))):
                runner(prompt)
            after = _brief_stamp(brief)
            if after is None or after == before:
                raise IntakeError(f"the agent did not write the brief at {brief}")
            _sidecar(brief).write_text(json.dumps({"item": item, "prompt_sha256": digest}), encoding="utf-8")
    decisions = parse_open_decisions(brief.read_text(encoding="utf-8"))
    if decisions:
        rec.set_checkpoint(Checkpoint.C1.value)
    elif rec.checkpoint == Checkpoint.C1.value:
        rec.set_checkpoint(None)
    return IntakeResult(brief_path=brief, skipped=skipped, open_decisions=decisions)
