"""Read-only GitHub helpers for the auto-approve command.

Holds the timing gate (FR-5): neither marking files viewed nor approving may
happen until the PR author has acted after the review comments the tool left; and the
approval safety check (FR-8), both read-only; and the two GitHub writes (FR-4 viewed marking,
FR-6 approval), which take ``dry_run`` and make no write call when it is set.

FR-8 decisions: OD-1 ``mergeable == UNKNOWN`` (GitHub still computing) is a SKIP, retried on
the next run, because approving on partial information is the unsafe direction; only
CONFLICTING is named by D-16 but UNKNOWN is not a positive "no conflicts" signal. OD-2 an
APPROVED review by the acting account on ANY commit counts as "already approved" (FR-6.4,
D-14): re-approval after a later push is manual.
OD-3 any check status other than pass/skipping blocks with the single reason "CI blocked", judged on the
latest run of each check only. Reading the checks (latest run per check, the Phase 3b stack-root rule and its
fail-closed cases) and the CI verdict live in ``ci_checks`` (``ci_blocker``). A PR with no checks passes only when
the repo has no workflow (empty is also what a host reports before a push's workflows register).

Every host call goes through ``CodeHost`` (``host``, ``repo``); nothing here builds a ``gh`` command or reads a raw
host dictionary. A read that fails (``CodeHostError``) gives ``None`` and so READ_FAILED; an unsupported capability
follows the policy table in ``common`` (stop re-raises; the viewed-state calls degrade).

FR-6.4: approval idempotency uses a new tri-state read (``_read_approval_state``) instead of
``review_requested._has_user_approved`` (which returns False on a read error); a read failure
never leads to a write. As in the safety check (shared ``_already_approved``), any APPROVED
review by the acting account, regardless of commit_id, counts as "already approved". The
head-moved guard and the head-revision binding on the approval are kept.

OD-1 (commit clock): the gate uses ``CommitInfo.committed_at`` from ``ChangeRequests.commits``,
counting only commits whose ``authors`` include the PR author. The committed date is the
closest clock to push time that needs no extra API call. A rebase/amend rewrites it to
"now", so a force-pushed commit normally lands after T and counts. A force-push that
preserves an old committer date (at or before T) is NOT detected and yields NOT_PASSED,
which is the safe direction: the gate fails closed and the author's later comment still
passes it. The authored date is ignored because rebases keep it unchanged.

OD-2 (inferred readings): bot activity is ignored when ``User.is_bot`` or the login
ends with ``[bot]``; "once the comments exist" means all of the summary, design and
traceability comments exist.
"""

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from dotharness.code_host import (
    ChangeRequest,
    CodeHost,
    CodeHostError,
    Comment,
    CommitInfo,
    RepositoryRef,
    Review,
    ReviewState,
    UnsupportedCapabilityError,
    User,
)
from dotharness.pr_review.runners.auto_approve_size import SizeEvaluation
from dotharness.pr_review.runners.ci_checks import (
    change_request_skip_reason,
    ci_blocker,
    guarded_read,
    read_change_request,
)
from dotharness.pr_review.runners.common import (
    call_with_policy,
    is_design_review_comment,
    is_inline_review_comment,
    is_review_summary_comment,
    is_traceability_review_comment,
)

logger = logging.getLogger(__name__)

REASON_ZERO_FINDINGS = "zero-findings"
REASON_AUTHOR_ACTIVITY = "author-activity-after-T"
_SITE = "auto_approve"
_VIEWED_SITE = "auto_approve_viewed"


class GateStatus(Enum):
    PASSED = "passed"
    NOT_PASSED = "not_passed"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    reason: str


def _login(user: User | None) -> str:
    return (user.login if user else "").lower()


def _is_bot(user: User | None) -> bool:
    return user is not None and (user.is_bot or user.login.lower().endswith("[bot]"))


def _is_author_activity(user: User | None, pr_author: str, acting: str) -> bool:
    login = _login(user)
    return login == pr_author and login != acting and not _is_bot(user)


async def _fetch_pr_commits(host: CodeHost, repo: RepositoryRef, pr_number: int) -> list[CommitInfo] | None:
    found = await guarded_read(
        _SITE,
        "ChangeRequests.commits",
        lambda: host.change_requests.commits(repo, pr_number),
        f"PR #{pr_number}: reading commits",
    )
    return None if found is None else list(found)


def _author_commit_after(commits: list[CommitInfo], pr_author: str, t: datetime) -> bool:
    for commit in commits:
        logins = {a.login.lower() for a in commit.authors}
        if pr_author in logins and commit.committed_at is not None and commit.committed_at > t:
            return True
    return False


async def check_timing_gate(
    host: CodeHost, repo: RepositoryRef, pr_number: int, pr_author: str, current_user: str
) -> GateResult:
    """Read-only timing gate. Never raises for read problems: returns READ_FAILED."""
    try:
        result = await _check_timing_gate(host, repo, pr_number, pr_author, current_user)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.exception("PR #%d: timing gate read raised", pr_number)
        result = GateResult(GateStatus.READ_FAILED, "read raised an exception")
    logger.info("PR #%d: timing gate %s (%s)", pr_number, result.status.value, result.reason)
    return result


def _scan_issue_comments(issue_comments: list[Comment], acting: str) -> tuple[bool, bool, bool, list[datetime]]:
    has_summary = has_design = has_traceability = False
    qualifying: list[datetime] = []
    for comment in issue_comments:
        if _login(comment.author) != acting:
            continue
        is_summary = is_review_summary_comment(comment.body)
        is_design = is_design_review_comment(comment.body)
        is_trace = is_traceability_review_comment(comment.body)
        if not (is_summary or is_design or is_trace):
            continue
        qualifying.append(comment.created_at)
        has_summary |= is_summary
        has_design |= is_design
        has_traceability |= is_trace
    return has_summary, has_design, has_traceability, qualifying


def _is_review_inline_comment(body: str) -> bool:
    """An inline comment from any review pass: the design and traceability passes tag theirs with
    their own marker, not the file pass's inline marker."""
    return is_inline_review_comment(body) or is_design_review_comment(body) or is_traceability_review_comment(body)


def _scan_inline_comments(review_comments: list[Comment], acting: str) -> list[datetime]:
    return [c.created_at for c in review_comments if _login(c.author) == acting and _is_review_inline_comment(c.body)]


def _scan_own_comments(
    issue_comments: list[Comment], review_comments: list[Comment], acting: str
) -> tuple[bool, bool, bool, int, list[datetime]]:
    """Scan the acting account's review comments.

    Returns (has_summary, has_design, has_traceability, inline_count, qualifying timestamps).
    """
    has_summary, has_design, has_traceability, qualifying = _scan_issue_comments(issue_comments, acting)
    inline = _scan_inline_comments(review_comments, acting)
    return has_summary, has_design, has_traceability, len(inline), qualifying + inline


def _has_activity_after(
    comments: list[Comment],
    review_comments: list[Comment],
    reviews: list[Review],
    t: datetime,
    author: str,
    acting: str,
) -> bool:
    for comment in (*comments, *review_comments):
        if comment.created_at > t and _is_author_activity(comment.author, author, acting):
            return True
    for review in reviews:
        # submitted_at, not a creation time; None while the review is pending
        if (
            review.submitted_at is not None
            and review.submitted_at > t
            and _is_author_activity(review.author, author, acting)
        ):
            return True
    return False


async def _read_timing_inputs(
    host: CodeHost, repo: RepositoryRef, pr_number: int
) -> tuple[list[Comment], list[Comment], list[Review]] | str:
    """The change-request comments, the inline review comments (all threads, flat) and the reviews; else the
    label of the read that failed."""
    comments = await guarded_read(
        _SITE,
        "Discussion.comments",
        lambda: host.discussion.comments(repo, pr_number),
        f"PR #{pr_number}: reading comments",
    )
    if comments is None:
        return "issue comments read failed"
    threads = await guarded_read(
        _SITE,
        "Discussion.review_threads",
        lambda: host.discussion.review_threads(repo, pr_number),
        f"PR #{pr_number}: reading review threads",
    )
    if threads is None:
        return "review comments read failed"
    reviews = await _read_reviews(host, repo, pr_number)
    if reviews is None:
        return "reviews read failed"
    return list(comments), [c for thread in threads for c in thread.comments], reviews


async def _read_reviews(host: CodeHost, repo: RepositoryRef, pr_number: int) -> list[Review] | None:
    found = await guarded_read(
        _SITE,
        "ChangeRequests.reviews",
        lambda: host.change_requests.reviews(repo, pr_number),
        f"PR #{pr_number}: reading reviews",
    )
    return None if found is None else list(found)


async def _check_timing_gate(
    host: CodeHost, repo: RepositoryRef, pr_number: int, pr_author: str, current_user: str
) -> GateResult:
    acting = current_user.lower()
    author = pr_author.lower()
    if not acting or not author:
        return GateResult(GateStatus.READ_FAILED, "acting account or PR author unknown")

    inputs = await _read_timing_inputs(host, repo, pr_number)
    if isinstance(inputs, str):
        return GateResult(GateStatus.READ_FAILED, inputs)
    issue_comments, review_comments, reviews = inputs

    has_summary, has_design, has_traceability, inline_count, qualifying = _scan_own_comments(
        issue_comments, review_comments, acting
    )

    if not (has_summary and has_design and has_traceability):
        return GateResult(GateStatus.NOT_PASSED, "summary, design and traceability comments not all present")
    if inline_count == 0:
        return GateResult(GateStatus.PASSED, REASON_ZERO_FINDINGS)

    t = max(qualifying)
    logger.info("PR #%d: newest review comment T=%s", pr_number, t.isoformat())

    if _has_activity_after(issue_comments, review_comments, reviews, t, author, acting):
        return GateResult(GateStatus.PASSED, REASON_AUTHOR_ACTIVITY)

    commits = await _fetch_pr_commits(host, repo, pr_number)
    if commits is None:
        return GateResult(GateStatus.READ_FAILED, "commits read failed")
    if _author_commit_after(commits, author, t):
        return GateResult(GateStatus.PASSED, REASON_AUTHOR_ACTIVITY)
    return GateResult(GateStatus.NOT_PASSED, "no author activity after T")


class SafetyStatus(Enum):
    SAFE = "safe"
    SKIP = "skip"
    READ_FAILED = "read_failed"


REASON_ALREADY_APPROVED = "already-approved"


@dataclass(frozen=True)
class SafetyResult:
    status: SafetyStatus
    reason: str
    head_sha: str = ""


def _already_approved(reviews: list[Review], acting: str) -> bool:
    """Any APPROVED review by the acting account, on any commit (FR-6.4, D-14)."""
    return any(r.state is ReviewState.APPROVED and _login(r.author) == acting for r in reviews)


async def _read_head(host: CodeHost, repo: RepositoryRef, pr_number: int) -> ChangeRequest | None:
    """The change request, fresh; None when unreadable or when it carries no head revision."""
    change_request = await read_change_request(host, repo, pr_number, _SITE)
    return change_request if change_request is not None and change_request.head_revision else None


async def check_approval_safety(host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str) -> SafetyResult:
    """Read-only approval safety check (FR-8). Never raises for read problems: returns READ_FAILED."""
    try:
        result = await _check_approval_safety(host, repo, pr_number, current_user)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.exception("PR #%d: approval safety read raised", pr_number)
        result = SafetyResult(SafetyStatus.READ_FAILED, "read raised an exception")
    logger.info(
        "PR #%d: approval safety %s (%s) head=%s", pr_number, result.status.value, result.reason, result.head_sha
    )
    return result


async def _check_approval_safety(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> SafetyResult:
    acting = current_user.lower()
    if not acting:
        return SafetyResult(SafetyStatus.READ_FAILED, "acting account unknown")
    change_request = await _read_head(host, repo, pr_number)
    if change_request is None:
        return SafetyResult(SafetyStatus.READ_FAILED, "PR state read failed")
    head = change_request.head_revision
    logger.info(
        "PR #%d: state=%s draft=%s mergeable=%s autoMerge=%s",
        pr_number,
        change_request.state.value,
        change_request.is_draft,
        change_request.mergeable.value if change_request.mergeable else None,
        change_request.auto_merge_enabled,
    )
    reason = change_request_skip_reason(change_request)
    if reason:
        return SafetyResult(SafetyStatus.SKIP, reason, head)

    reviews = await _read_reviews(host, repo, pr_number)
    if reviews is None:
        return SafetyResult(SafetyStatus.READ_FAILED, "reviews read failed", head)
    if _already_approved(reviews, acting):
        return SafetyResult(SafetyStatus.SKIP, REASON_ALREADY_APPROVED, head)

    block = await ci_blocker(host, repo, pr_number, change_request.base_reference)
    if block is not None:
        return SafetyResult(SafetyStatus.READ_FAILED if block.read_failed else SafetyStatus.SKIP, block.reason, head)
    return SafetyResult(SafetyStatus.SAFE, "all safety checks passed", head)


# ---------------------------------------------------------------------------
# Writes (FR-4 viewed marking, FR-6 approval). Every write honours dry_run.
# ---------------------------------------------------------------------------


class ViewedStatus(Enum):
    DONE = "done"  # reads succeeded; see marked / failed
    GATE_NOT_PASSED = "gate_not_passed"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class ViewedResult:
    status: ViewedStatus
    marked: list[str] = field(default_factory=list)  # marked, or would be marked in dry-run
    skipped: list[str] = field(default_factory=list)  # already viewed
    failed: list[str] = field(default_factory=list)  # a mark call failed
    all_viewed: bool = False  # every file of the PR is viewed (or was just marked, or would be in dry-run)


class ApprovalStatus(Enum):
    APPROVED = "approved"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"
    READ_FAILED = "read_failed"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class ApprovalResult:
    status: ApprovalStatus
    reason: str


REASON_GATE_NOT_PASSED = "gate-not-passed"
REASON_SAFETY_NOT_PASSED = "safety-not-passed"
REASON_TOO_LARGE = "too-large"
REASON_NO_HEAD_SHA = "no-head-sha"
REASON_HEAD_MOVED = "head-moved"
REASON_DRY_RUN = "dry-run"


async def read_viewed_states(host: CodeHost, repo: RepositoryRef, pr_number: int) -> dict[str, bool] | None:
    """{path: viewed} for the acting user, or None on any read failure.

    A host without viewed state (``UnsupportedCapabilityError`` under the degrade policy) gives ``{}``: nothing to
    mark and no file counts as viewed, so approval falls back to the size limit.
    """
    try:
        found = await call_with_policy(
            _VIEWED_SITE,
            "ChangeRequests.viewed_files",
            lambda: host.change_requests.viewed_files(repo, pr_number),
            {},
        )
    except UnsupportedCapabilityError:
        raise
    except CodeHostError:
        logger.exception("PR #%d: viewed files read failed", pr_number)
        return None
    return dict(found)


async def _mark_one(host: CodeHost, repo: RepositoryRef, pr_number: int, path: str) -> bool | None:
    """True when marked, False when the write failed (never retried: it may have taken effect), None when the host
    cannot mark files (degrade policy)."""

    async def mark() -> bool:
        await host.change_requests.mark_viewed(repo, pr_number, path)
        return True

    try:
        return await call_with_policy(_VIEWED_SITE, "ChangeRequests.mark_viewed", mark, None)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError:
        logger.exception("PR #%d: marking %s viewed failed", pr_number, path)
        return False


def _mark_candidates(
    file_counts: dict[str, int], viewed_max_lines: int, states: dict[str, bool]
) -> tuple[list[str], list[str]]:
    """Split counted files strictly below the threshold into (to mark, already viewed).

    Files the PR file list does not contain are left alone."""
    to_mark: list[str] = []
    already: list[str] = []
    for path, lines in file_counts.items():
        if lines >= viewed_max_lines or path not in states:
            continue
        (already if states[path] else to_mark).append(path)
    return to_mark, already


async def mark_files_viewed(
    host: CodeHost,
    repo: RepositoryRef,
    pr_number: int,
    file_counts: dict[str, int],
    viewed_max_lines: int,
    gate_passed: bool,
    dry_run: bool = False,
) -> ViewedResult:
    """Mark counted files strictly below ``viewed_max_lines`` as viewed (FR-4). Never raises for read problems.

    No write unless ``gate_passed``; no write if the file/state read failed; a failed individual
    mark is logged and does not stop the rest. Marking does not depend on CI or PR size.
    """
    if not gate_passed:
        logger.info("PR #%d: timing gate not passed, not marking files viewed", pr_number)
        return ViewedResult(ViewedStatus.GATE_NOT_PASSED)
    try:
        states = await read_viewed_states(host, repo, pr_number)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.exception("PR #%d: viewed state read raised", pr_number)
        states = None
    if states is None:
        return ViewedResult(ViewedStatus.READ_FAILED)
    to_mark, already = _mark_candidates(file_counts, viewed_max_lines, states)
    marked: list[str] = []
    failed: list[str] = []
    for path in to_mark:
        if dry_run:
            logger.info("PR #%d: [dry-run] would mark %s viewed", pr_number, path)
            marked.append(path)
            continue
        outcome = await _mark_one(host, repo, pr_number, path)
        if outcome:
            logger.info("PR #%d: marked %s viewed", pr_number, path)
            marked.append(path)
        elif outcome is False:
            failed.append(path)
    for path in already:
        logger.info("PR #%d: %s already viewed, skipping", pr_number, path)
    viewed_now = {*marked, *(p for p, viewed in states.items() if viewed)}
    all_viewed = bool(states) and all(path in viewed_now for path in states)
    return ViewedResult(ViewedStatus.DONE, marked=marked, skipped=already, failed=failed, all_viewed=all_viewed)


async def _read_approval_state(host: CodeHost, repo: RepositoryRef, pr_number: int, acting: str) -> bool | None:
    """Tri-state: True = acting account already approved (any commit), False = not, None = read failed."""
    reviews = await _read_reviews(host, repo, pr_number)
    if reviews is None:
        return None
    return _already_approved(reviews, acting)


def _approval_precheck(
    gate: GateResult, safety: SafetyResult, total_lines: int, approve_max_lines: int, all_viewed: bool
) -> ApprovalResult | None:
    if gate.status is not GateStatus.PASSED:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_GATE_NOT_PASSED)
    if safety.status is not SafetyStatus.SAFE:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_SAFETY_NOT_PASSED)
    if not safety.head_sha:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_NO_HEAD_SHA)
    if not all_viewed and total_lines >= approve_max_lines:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_TOO_LARGE)
    return None


async def _post_approval(host: CodeHost, repo: RepositoryRef, pr_number: int, head_sha: str) -> bool:
    """Approve at ``head_sha``. A failure (including a timeout that may have taken effect) is logged and is False; the
    write is never retried or followed by a fallback."""

    async def approve() -> bool:
        await host.change_requests.approve(repo, pr_number, head_revision=head_sha)
        return True

    return await guarded_read(_SITE, "ChangeRequests.approve", approve, f"PR #{pr_number}: approval") is True


async def _approve(
    host: CodeHost, repo: RepositoryRef, pr_number: int, head_sha: str, current_user: str, dry_run: bool
) -> ApprovalResult:
    acting = current_user.lower()
    if not acting:
        return ApprovalResult(ApprovalStatus.READ_FAILED, "acting account unknown")
    approved = await _read_approval_state(host, repo, pr_number, acting)
    if approved is None:
        return ApprovalResult(ApprovalStatus.READ_FAILED, "reviews read failed")
    if approved:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_ALREADY_APPROVED)
    change_request = await _read_head(host, repo, pr_number)
    if change_request is None:
        return ApprovalResult(ApprovalStatus.READ_FAILED, "PR state read failed")
    if change_request.head_revision != head_sha:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_HEAD_MOVED)
    if dry_run:
        logger.info("PR #%d: [dry-run] would approve at %s", pr_number, head_sha)
        return ApprovalResult(ApprovalStatus.DRY_RUN, REASON_DRY_RUN)
    if not await _post_approval(host, repo, pr_number, head_sha):
        return ApprovalResult(ApprovalStatus.WRITE_FAILED, "approval POST failed")
    logger.info("PR #%d: approved at %s", pr_number, head_sha)
    return ApprovalResult(ApprovalStatus.APPROVED, "approved")


async def approve_pr(
    host: CodeHost,
    repo: RepositoryRef,
    pr_number: int,
    gate: GateResult,
    safety: SafetyResult,
    size: SizeEvaluation,
    approve_max_lines: int,
    current_user: str,
    dry_run: bool = False,
    all_viewed: bool = False,
) -> ApprovalResult:
    """Approve a PR strictly below ``approve_max_lines``, or any size once every file is viewed (FR-6).

    ``all_viewed`` (every file of the PR marked viewed, by the reviewer or by this tool) waives only
    the size limit; the gate, safety, idempotency and head-binding rules still apply. Never raises
    for read problems.

    Owns the whole decision: requires ``gate`` PASSED and ``safety`` SAFE, binding the approval to
    ``safety.head_sha``. Re-reads reviews (tri-state) and the current head; any failed read or
    moved head means no write. The review body is empty and the approval is made at the head revision.
    """
    result = _approval_precheck(gate, safety, size.total_lines, approve_max_lines, all_viewed)
    if result is None:
        try:
            result = await _approve(host, repo, pr_number, safety.head_sha, current_user, dry_run)
        except (subprocess.SubprocessError, OSError, ValueError):
            logger.exception("PR #%d: approval raised", pr_number)
            result = ApprovalResult(ApprovalStatus.READ_FAILED, "approval raised an exception")
    return result
