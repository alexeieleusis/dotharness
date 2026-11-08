"""Branch scoping for the cleanup-pr command (FR-4 issue selection, FR-5 duplication selection).

Glue that maps ``IssueLineFilter`` output (``ReviewIssue`` objects) back to
the ``SonarQubeIssue`` records that produced it, so the fixer works on full
issue objects (key, status, ...) instead of the filtered report views, and
that selects the duplication groups whose target block intersects the branch's
strict changed lines — the same active-duplication rule ``review`` applies.
"""

import dataclasses

from dotharness.vibe_heal.deduplication.models import DuplicationGroup
from dotharness.vibe_heal.review.duplication_scope import changed_lines_in_block
from dotharness.vibe_heal.review.line_filter import IssueLineFilter
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


@dataclasses.dataclass
class IssueScopeResult:
    """Branch-scoped issue selection result for one file."""

    in_scope: list[SonarQubeIssue]
    """Fixable issues the line filter kept; shaped for the FR-7 ``issue_filter`` hook on ``fix_file``."""
    out_of_scope_count: int
    """Fixable issues dropped by the line filter (fixable but not kept)."""


def select_in_scope_issues(
    issues: list[SonarQubeIssue],
    new_lines: set[int],
    strict_new_lines: set[int],
) -> IssueScopeResult:
    """Select the in-scope, fixable SonarQube issues for a file.

    Runs :meth:`IssueLineFilter.filter_issues` unchanged against the
    already-resolved line sets for the target file and maps the surviving
    ``ReviewIssue`` objects back to their source ``SonarQubeIssue`` records
    by ``(rule, line)``. No fetching or path conversion happens here; the
    caller (WO-2b) resolves the sets from ``DiffLines`` and fetches issues.

    Args:
        issues: SonarQube issues for the file.
        new_lines: Trailing-window line set for the repo-relative path
            (``DiffLines.new_lines``); the set ``IssueLineFilter`` matches against.
        strict_new_lines: Strict changed-line set for the repo-relative path
            (``DiffLines.strict_new_lines``); passed through unchanged so the
            filter can mark window-only issues with ``on_changed_line=False``.

    Returns:
        An :class:`IssueScopeResult` whose ``in_scope`` holds every fixable
        issue matching a surviving ``(rule, line)`` pair — a pair can match
        more than one record (same rule, same line) and all matches are kept,
        no dedup — and whose ``out_of_scope_count`` is the number of fixable
        issues the filter dropped (fixable but not kept).
    """
    surviving = IssueLineFilter.filter_issues(
        issues,
        new_lines,
        strict_changed_lines=strict_new_lines,
    )
    surviving_pairs = {(review_issue.rule, review_issue.line) for review_issue in surviving}

    kept = [
        issue
        for issue in issues
        if issue.is_fixable and issue.line is not None and (issue.rule, issue.line) in surviving_pairs
    ]
    fixable_count = sum(1 for issue in issues if issue.is_fixable)

    return IssueScopeResult(in_scope=kept, out_of_scope_count=fixable_count - len(kept))


@dataclasses.dataclass
class DuplicationScopeResult:
    """Branch-scoped duplication selection result for one file."""

    in_scope: list[DuplicationGroup]
    """Groups whose target block intersects the strict changed lines; shaped for the FR-7 ``group_filter`` hook on ``dedupe_file``."""
    out_of_scope_count: int
    """Groups considered but not kept (no target block, or target block misses the strict changed lines)."""


def select_in_scope_duplications(
    groups: list[DuplicationGroup],
    target_ref: str,
    strict_new_lines: set[int],
) -> DuplicationScopeResult:
    """Select the in-scope duplication groups for a file.

    Keeps every group whose target block ``[from_line, to_line]`` (inclusive)
    intersects ``strict_new_lines``, via :func:`changed_lines_in_block` — the
    same active-duplication intersection test ``ReviewOrchestrator._build_active_finding``
    uses, so ``cleanup-pr`` and ``review`` cannot disagree. The **strict** set
    is used, not the 3-line trailing window, which makes the rule stricter
    than the issue line filter. No fetching or path conversion happens here;
    the caller (WO-2b) resolves the set from ``DiffLines.strict_new_lines``
    and fetches the groups from the temp project.

    Args:
        groups: Duplication groups for the file.
        target_ref: File reference ID of the target file in the groups' files map.
        strict_new_lines: Strict changed-line set for the repo-relative path
            (``DiffLines.strict_new_lines``); the set a target block must intersect.

    Returns:
        A :class:`DuplicationScopeResult` whose ``in_scope`` holds the kept
        groups and whose ``out_of_scope_count`` is groups considered minus
        groups kept.
    """
    kept: list[DuplicationGroup] = []
    for group in groups:
        target_block = group.get_target_block(target_ref)
        if target_block is not None and changed_lines_in_block(target_block, strict_new_lines):
            kept.append(group)

    return DuplicationScopeResult(in_scope=kept, out_of_scope_count=len(groups) - len(kept))
