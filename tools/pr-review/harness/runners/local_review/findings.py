"""Finding blocks: parsing, id stamping and the `AddressFinding` carrier.

Standard library only: this module must import without `harness.backend`, `harness.lock`
or any orchestrator.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

FINDING_HEADING = "## Finding:"
VALID_SEVERITIES = ("P0", "P1")
VALID_STATUSES = ("open", "wontfix", "fixed", "declined")
VALID_PASSES = ("file", "design")
_FIELD_RE = re.compile(r"^-\s*([A-Za-z_]+)\s*:\s*(.*?)\s*$")
_ID_RE = re.compile(r"(file|design)-[0-9a-f]{8}")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_H2_RE = re.compile(r"^## ")
STATUS_FIELD_RE = re.compile(r"^-\s*status\s*:", re.IGNORECASE)
COMMIT_FIELD_RE = re.compile(r"^-\s*commit\s*:", re.IGNORECASE)


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
        while pos < blk.end_line and not lines[pos].strip():  # mirror `_metadata`: skip leading blanks
            pos += 1
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
