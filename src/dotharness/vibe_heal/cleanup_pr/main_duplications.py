"""Main-duplication detection helpers for the cleanup-pr command (FR-6).

A *main duplication* is a duplication block that existed in the base branch, was
modified or removed by the branch, and is no longer active in the branch
(``ResolvedDuplication``). This module reads the main-side text and the
branch-side hunk from git and builds the AI prompt and commit message for one
qualifying group. Detection itself reuses ``review.duplication_scope``; the
orchestration (ordering, counters, failure policy) lives in ``orchestrator.py``.
"""

import dataclasses
import re
from pathlib import Path

from git import GitCommandError, Repo

from dotharness.vibe_heal.review.models import ResolvedDuplication

_SNIPPET_THRESHOLD = 6
_SNIPPET_EDGE = 3
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclasses.dataclass
class MainDuplicationTask:
    """One qualifying main duplication, ready to be fixed."""

    file_path: Path
    """CWD-relative path of the branch file (the file handed to the AI tool)."""
    repo_relative: str
    """Repo-root-relative POSIX path (key into the diff maps, path in the main ref)."""
    resolved: ResolvedDuplication
    main_block_text: str
    """Main-side block text (first-3 / last-3 snippet rule applied), with line numbers."""
    branch_hunk: str
    """Unified-diff hunk(s) of the branch's change around the anchor."""
    branch_range: tuple[int, int]
    """New-side (HEAD) line range covered by ``branch_hunk``."""


def read_main_file_lines(repo: Repo, merge_base: str, repo_relative: str) -> list[str] | None:
    """Read a file as it was at the merge base (``git show <merge-base>:<path>``).

    Returns:
        The file's lines, or None when the file does not exist at the merge base.
    """
    try:
        content = repo.git.show(f"{merge_base}:{repo_relative}")
    except GitCommandError:
        return None
    return str(content).splitlines()


def format_block_snippet(lines: list[str], from_line: int, to_line: int) -> str:
    """Format lines ``from_line..to_line`` (1-indexed, inclusive) as a numbered snippet.

    Blocks of at most 6 lines are shown in full; longer blocks show the first 3
    and last 3 lines with the omitted count, the same rule the dedupe prompt uses.
    """
    block = lines[from_line - 1 : to_line]
    size = len(block)
    if size <= _SNIPPET_THRESHOLD:
        numbered = [f"{i}: {line.rstrip()}" for i, line in enumerate(block, start=from_line)]
        return "Code block:\n" + "\n".join(numbered)

    first = [f"{i}: {line.rstrip()}" for i, line in enumerate(block[:_SNIPPET_EDGE], start=from_line)]
    last_start = from_line + size - _SNIPPET_EDGE
    last = [f"{i}: {line.rstrip()}" for i, line in enumerate(block[-_SNIPPET_EDGE:], start=last_start)]
    omitted = size - 2 * _SNIPPET_EDGE
    return (
        "First 3 lines:\n"
        + "\n".join(first)
        + f"\n\n... ({omitted} lines omitted) ...\n\nLast 3 lines:\n"
        + "\n".join(last)
    )


def extract_branch_hunks(
    diff_text: str, main_from: int, main_to: int, anchor_new_line: int
) -> tuple[str, tuple[int, int]] | None:
    """Limit a ``--unified=0`` diff of one file to the hunks around a main block.

    A hunk is kept when its old-side span intersects ``[main_from, main_to]`` or
    its new-side span contains ``anchor_new_line``. If none match, None is
    returned: a hunk touching neither the main block nor the anchor is an
    unrelated change (or a rename/copy artifact) and must not be presented as
    the branch change to re-apply.

    Returns:
        ``(hunk_text, (first_new_line, last_new_line))`` or None when the diff has
        no hunks or no hunk relates to the main block.
    """
    header: list[str] = []
    hunks: list[tuple[int, int, int, int, list[str]]] = []  # old_start, old_end, new_start, new_end, lines
    for line in diff_text.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            hunks.append((
                old_start,
                old_start + max(old_count, 1) - 1,
                new_start,
                new_start + max(new_count, 1) - 1,
                [line],
            ))
        elif hunks:
            hunks[-1][4].append(line)
        elif line.startswith(("--- ", "+++ ")):
            header.append(line)

    if not hunks:
        return None

    kept = [h for h in hunks if (h[0] <= main_to and h[1] >= main_from) or (h[2] <= anchor_new_line <= h[3])]
    if not kept:
        # No hunk touches the main block (old side) nor the anchor (new side). Rather than
        # fall back to the nearest hunk — which may be an unrelated edit far away, or a
        # whole-file add from rename/copy detection — return None so the caller skips
        # the group and counts it in ``main_duplications_skipped``.
        return None

    text = "\n".join(header + [ln for h in kept for ln in h[4]])
    return text, (min(h[2] for h in kept), max(h[3] for h in kept))


def build_main_duplication_task(
    repo: Repo,
    merge_base: str,
    file_path: Path,
    repo_relative: str,
    resolved: ResolvedDuplication,
) -> MainDuplicationTask | None:
    """Read the main-side block and the branch-side hunk for one resolved duplication.

    Returns:
        The task, or None when the file is missing at the merge base, has no diff, or no
        branch hunk relates to the main block.
    """
    main_lines = read_main_file_lines(repo, merge_base, repo_relative)
    if main_lines is None:
        return None
    try:
        # ``--no-renames`` keeps the diff scoped to this exact path pair. With rename/copy
        # detection on, the diff could surface as a whole-file add whose hunks have no
        # relation to the main block.
        diff_text = str(
            repo.git.diff("--no-color", "--no-renames", "--unified=0", merge_base, "HEAD", "--", repo_relative)
        )
    except GitCommandError:
        return None
    hunks = extract_branch_hunks(diff_text, resolved.main_from_line, resolved.main_to_line, resolved.anchor_new_line)
    if hunks is None:
        return None
    hunk_text, branch_range = hunks
    return MainDuplicationTask(
        file_path=file_path,
        repo_relative=repo_relative,
        resolved=resolved,
        main_block_text=format_block_snippet(main_lines, resolved.main_from_line, resolved.main_to_line),
        branch_hunk=hunk_text,
        branch_range=branch_range,
    )


def _other_locations_text(task: MainDuplicationTask) -> str:
    return "\n".join(
        f"  - {loc.file_path} (lines {loc.from_line}-{loc.to_line})" for loc in task.resolved.other_locations
    )


def build_main_duplication_prompt(task: MainDuplicationTask) -> str:
    """Build the AI prompt for one main duplication (FR-6 Fix, items 1-4)."""
    resolved = task.resolved
    size = resolved.main_to_line - resolved.main_from_line + 1
    parts = [
        f"A duplication that existed in the base branch (main) was modified or removed by this branch. "
        f"In main it spanned lines {resolved.main_from_line}-{resolved.main_to_line} ({size} lines) "
        f"of {task.repo_relative}.\n",
        "Main-side block (as it exists in main):",
        task.main_block_text,
    ]

    if resolved.other_locations:
        parts.append(f"\n\nThe same code is duplicated in {len(resolved.other_locations)} other location(s):")
        parts.append(_other_locations_text(task))
    else:
        parts.append("\n\nNo other locations were reported.")

    parts.append(
        f"\n\nThe branch changed this file around lines {task.branch_range[0]}-{task.branch_range[1]} "
        "(unified diff, main to branch):"
    )
    parts.append(task.branch_hunk)

    parts.append(
        "\n\nDo these steps in order:\n"
        "1. Refactor the duplication as it exists in main into a shared helper, and update ALL other "
        "locations listed above to use it.\n"
        "2. Then re-apply the branch's change (the diff above) on top of the shared helper, preserving "
        "the branch's behavior.\n"
        "3. Do not change behavior beyond what the diff already does."
    )
    return "\n".join(parts)


def build_main_duplication_commit_message(task: MainDuplicationTask, ai_tool_name: str) -> str:
    """Build the commit message for one main-duplication refactor (FR-6 Commits)."""
    resolved = task.resolved
    size = resolved.main_to_line - resolved.main_from_line + 1
    other = _other_locations_text(task) or "  (none reported)"
    return (
        f"refactor: [duplication] extract shared code from removed main duplication at line "
        f"{resolved.main_from_line}\n\n"
        f"Extracted shared code from a duplication that existed in main and was modified or removed "
        f"by this branch.\n\n"
        f"Main-side block:\n"
        f"  - {task.repo_relative} (lines {resolved.main_from_line}-{resolved.main_to_line}, {size} lines)\n\n"
        f"Other locations:\n{other}\n\n"
        f"Branch-side changed range:\n"
        f"  - {task.repo_relative} (lines {task.branch_range[0]}-{task.branch_range[1]})\n\n"
        f"AI tool: {ai_tool_name}\n\n"
        f"[vibe-heal](https://github.com/alexeieleusis/vibe-heal)"
    )
