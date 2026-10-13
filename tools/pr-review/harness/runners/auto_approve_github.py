"""Read-only GitHub helpers for the auto-approve command.

Currently holds the timing gate (FR-5): neither marking files viewed nor approving may
happen until the PR author has acted after the review comments the tool left. Nothing
here makes a GitHub write call.

OD-1 (commit clock): the gate uses ``committedDate`` from ``gh pr view --json commits``,
counting only commits whose ``authors`` include the PR author. ``committedDate`` is the
closest clock to push time that needs no extra API call. A rebase/amend rewrites it to
"now", so a force-pushed commit normally lands after T and counts. A force-push that
preserves an old committer date (at or before T) is NOT detected and yields NOT_PASSED,
which is the safe direction: the gate fails closed and the author's later comment still
passes it. ``authoredDate`` is ignored because rebases keep it unchanged.

OD-2 (inferred readings): bot activity is ignored when ``user.type == "Bot"`` or the login
ends with ``[bot]``; "once the comments exist" means all of the summary, design and
traceability comments exist.
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from harness.runners.common import (
    TIMEOUT_GH,
    fetch_all_pages,
    is_design_review_comment,
    is_inline_review_comment,
    is_review_summary_comment,
    is_traceability_review_comment,
    run_cmd,
)

logger = logging.getLogger(__name__)

REASON_ZERO_FINDINGS = "zero-findings"
REASON_AUTHOR_ACTIVITY = "author-activity-after-T"


class GateStatus(Enum):
    PASSED = "passed"
    NOT_PASSED = "not_passed"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    reason: str


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _login(item: dict) -> str:
    return ((item.get("user") or {}).get("login") or "").lower()


def _is_bot(item: dict) -> bool:
    user = item.get("user") or {}
    return user.get("type") == "Bot" or (user.get("login") or "").lower().endswith("[bot]")


def _is_author_activity(item: dict, pr_author: str, acting: str) -> bool:
    login = _login(item)
    return login == pr_author and login != acting and not _is_bot(item)


def _fetch_pr_commits(repo: str, pr_number: int, env: dict) -> list[dict] | None:
    result = run_cmd(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "commits"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        commits = json.loads(result.stdout).get("commits")
    except (ValueError, AttributeError):
        return None
    return commits if isinstance(commits, list) else None


def _author_commit_after(commits: list[dict], pr_author: str, t: datetime) -> bool:
    for commit in commits:
        logins = {(a.get("login") or "").lower() for a in commit.get("authors") or []}
        date = _parse_ts(commit.get("committedDate"))
        if pr_author in logins and date is not None and date > t:
            return True
    return False


def check_timing_gate(repo: str, pr_number: int, pr_author: str, current_user: str, env: dict) -> GateResult:
    """Read-only timing gate. Never raises for read problems: returns READ_FAILED."""
    try:
        result = _check_timing_gate(repo, pr_number, pr_author, current_user, env)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.exception("PR #%d: timing gate read raised", pr_number)
        result = GateResult(GateStatus.READ_FAILED, "read raised an exception")
    logger.info("PR #%d: timing gate %s (%s)", pr_number, result.status.value, result.reason)
    return result


def _scan_own_comments(
    issue_comments: list[dict], review_comments: list[dict], acting: str
) -> tuple[bool, bool, bool, int, list[datetime]] | str:
    """Scan the acting account's review comments.

    Returns (has_summary, has_design, has_traceability, inline_count, qualifying timestamps),
    or a failure reason string when a qualifying comment has no valid created_at.
    """
    has_summary = has_design = has_traceability = False
    inline_count = 0
    qualifying: list[datetime] = []
    for comment in issue_comments:
        if _login(comment) != acting:
            continue
        body = comment.get("body") or ""
        is_summary = is_review_summary_comment(body)
        is_design = is_design_review_comment(body)
        is_trace = is_traceability_review_comment(body)
        if not (is_summary or is_design or is_trace):
            continue
        ts = _parse_ts(comment.get("created_at"))
        if ts is None:
            return "qualifying comment has no valid created_at"
        qualifying.append(ts)
        has_summary |= is_summary
        has_design |= is_design
        has_traceability |= is_trace
    for comment in review_comments:
        if _login(comment) == acting and is_inline_review_comment(comment.get("body") or ""):
            ts = _parse_ts(comment.get("created_at"))
            if ts is None:
                return "inline comment has no valid created_at"
            qualifying.append(ts)
            inline_count += 1
    return has_summary, has_design, has_traceability, inline_count, qualifying


def _has_activity_after(
    comments: list[dict], review_comments: list[dict], reviews: list[dict], t: datetime, author: str, acting: str
) -> bool:
    for item in (*comments, *review_comments):
        ts = _parse_ts(item.get("created_at"))
        if ts is not None and ts > t and _is_author_activity(item, author, acting):
            return True
    for review in reviews:
        ts = _parse_ts(review.get("submitted_at"))  # submitted_at, not created_at; None while PENDING
        if ts is not None and ts > t and _is_author_activity(review, author, acting):
            return True
    return False


def _check_timing_gate(repo: str, pr_number: int, pr_author: str, current_user: str, env: dict) -> GateResult:
    failed = GateResult(GateStatus.READ_FAILED, "")
    acting = current_user.lower()
    author = pr_author.lower()
    if not acting or not author:
        return GateResult(GateStatus.READ_FAILED, "acting account or PR author unknown")

    endpoints = (
        ("issue comments", f"repos/{repo}/issues/{pr_number}/comments"),
        ("review comments", f"repos/{repo}/pulls/{pr_number}/comments"),
        ("reviews", f"repos/{repo}/pulls/{pr_number}/reviews"),
    )
    pages = []
    for label, path in endpoints:
        data = fetch_all_pages(path, env)
        if data is None:
            return GateResult(failed.status, f"{label} read failed")
        pages.append(data)
    issue_comments, review_comments, reviews = pages

    scanned = _scan_own_comments(issue_comments, review_comments, acting)
    if isinstance(scanned, str):
        return GateResult(failed.status, scanned)
    has_summary, has_design, has_traceability, inline_count, qualifying = scanned

    if not (has_summary and has_design and has_traceability):
        return GateResult(GateStatus.NOT_PASSED, "summary, design and traceability comments not all present")
    if inline_count == 0:
        return GateResult(GateStatus.PASSED, REASON_ZERO_FINDINGS)

    t = max(qualifying)
    logger.info("PR #%d: newest review comment T=%s", pr_number, t.isoformat())

    if _has_activity_after(issue_comments, review_comments, reviews, t, author, acting):
        return GateResult(GateStatus.PASSED, REASON_AUTHOR_ACTIVITY)

    commits = _fetch_pr_commits(repo, pr_number, env)
    if commits is None:
        return GateResult(failed.status, "commits read failed")
    if _author_commit_after(commits, author, t):
        return GateResult(GateStatus.PASSED, REASON_AUTHOR_ACTIVITY)
    return GateResult(GateStatus.NOT_PASSED, "no author activity after T")
