"""Auto-approve runner: marks small files viewed and approves small PRs (issue #95).

Public entry point (takes the shared working-directory lock itself, like the sibling runners)::

    run(config: HarnessConfig, *, dry_run: bool = False) -> None

* ``config.auto_approve.enabled`` false: returns immediately, no host/git call at all.
* Otherwise processes every open, non-draft PR with a pending review request for the acting user; each PR is
  isolated (a failure is logged and the loop continues).
* ``dry_run=True`` performs every read and check but no write: the sibling write functions
  in ``auto_approve_github`` (the only write points: ``markFileAsViewed`` and the review POST)
  receive ``dry_run`` and log what they would do.
* Raises :class:`AutoApproveError` if the PR list itself cannot be read (e.g. bad token), or after
  the loop if any PR hit a git error or an unexpected exception, so the CLI exits non-zero. A failed
  host read or write on a PR is logged at ERROR and does not fail the run; the PR is retried next run.

Per PR: size evaluation, timing gate, approval-safety reads (all reads before any write; any
read failure means no write on that PR), then mark small files viewed (gate open), then approve
(small PR, or any size once every file of the PR is viewed; safe, not already approved, bound to
the head SHA). The timing gate is what requires the author's comment (or push) after the review:
it applies to both paths, so a fully viewed PR is not approved before the author has responded.

A PR with no counted files (only renames, deletes, or binary files) is never approved and
nothing is marked: zero counted lines would otherwise satisfy "strictly below" trivially, and
nothing was actually reviewed by size. The brief gives no basis to approve it, so the safe
direction is taken and the skip is logged.

Size comes from ``git diff`` on a checkout of ``refs/pull/<n>/head`` rather than from the
``additions``/``deletions`` already returned by the files GraphQL query, because git gives an
explicit binary marker (numstat ``-``) whereas GraphQL reports 0/0 for binary files, which is
indistinguishable from a text file with no line changes and would be marked viewed. The checkout
also pins the measured commit to a SHA that is compared with the head SHA read for the approval
safety checks. Moving size to GraphQL (dropping the checkout, the lock dependency and the git
colour workaround) is a possible follow-up if the binary and head-binding cases are handled.
"""

import logging
import subprocess

from dotharness.code_host import (
    ChangeRequest,
    ChangeRequestState,
    CodeHost,
    CodeHostError,
    RepositoryRef,
    UnsupportedCapabilityError,
)
from dotharness.pr_review.config import HarnessConfig
from dotharness.pr_review.lock import acquire_lock
from dotharness.pr_review.runners.auto_approve_github import (
    ApprovalStatus,
    GateStatus,
    SafetyStatus,
    ViewedStatus,
    approve_pr,
    check_approval_safety,
    check_timing_gate,
    mark_files_viewed,
)
from dotharness.pr_review.runners.auto_approve_size import SizeEvaluation, evaluate_size
from dotharness.pr_review.runners.common import (
    TIMEOUT_GIT,
    FatalGitError,
    call_with_policy,
    get_head_sha,
    git_detach_and_record,
    git_restore,
    host_current_user,
    open_host,
    run_cmd,
)

logger = logging.getLogger(__name__)


class AutoApproveError(RuntimeError):
    """Raised by run() after the loop when one or more PRs failed."""


async def run(config: HarnessConfig, *, dry_run: bool = False) -> None:
    with acquire_lock(config.lock_key):
        await _run_locked(config, dry_run)


async def _run_locked(config: HarnessConfig, dry_run: bool) -> None:
    if not config.auto_approve.enabled:
        logger.info("auto-approve is disabled, nothing to do")
        return
    scaffold = await open_host(config)
    host, repo, env = scaffold.host, scaffold.repo, scaffold.env
    current_user = await host_current_user(host)
    prs = await _list_prs(host, repo, current_user)
    wdir = str(config.repo.working_dir)
    if not prs:
        logger.info(
            "auto-approve: no open PRs with a pending review request for %s on %s, nothing to do",
            current_user,
            await host.repository.display_name(repo),
        )

    failures: list[str] = []
    for pr in prs:
        if not _is_candidate(pr):
            continue
        pr_number = pr.number
        try:
            await _process_pr(pr, config, wdir, env, host, repo, current_user, dry_run)
        except FatalGitError as e:
            logger.exception("PR #%d: fatal git error", pr_number)
            failures.append(f"#{pr_number}: {e}")
            break
        except Exception as e:
            logger.exception("PR #%d: error", pr_number)
            failures.append(f"#{pr_number}: {e}")
            continue
    if failures:
        raise AutoApproveError("auto-approve failed for: " + "; ".join(failures))


async def _list_prs(host: CodeHost, repo: RepositoryRef, current_user: str) -> list[ChangeRequest]:
    """Open change requests with a pending review request for ``current_user``, sorted by number.

    A failed read raises :class:`AutoApproveError` (distinct from an empty list). An unsupported
    capability follows the ``auto_approve`` policy (stop) and propagates unwrapped.
    """
    if not current_user:
        raise AutoApproveError("cannot list review requests: the acting user is unknown")  # noqa: TRY003

    async def list_requested() -> list[ChangeRequest]:
        return list(
            await host.change_requests.list(repo, state=ChangeRequestState.OPEN, requested_reviewer=current_user)
        )

    try:
        found = await call_with_policy("auto_approve", "ChangeRequests.list", list_requested, [])
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as exc:
        raise AutoApproveError(f"listing change requests failed: {exc}") from exc  # noqa: TRY003
    return sorted(found, key=lambda c: c.number)


def _is_candidate(pr: ChangeRequest) -> bool:
    if pr.is_draft:
        logger.info("PR #%d: draft, skipping", pr.number)
        return False
    if pr.state is not ChangeRequestState.OPEN:
        logger.info("PR #%d: not open (%s), skipping", pr.number, pr.state.value)
        return False
    if pr.is_cross_repository:
        logger.info("PR #%d: from a fork, skipping", pr.number)
        return False
    if not pr.head_reference or not pr.base_reference:
        logger.info("PR #%d: missing head/base branch, skipping", pr.number)
        return False
    return True


async def _process_pr(
    pr: ChangeRequest,
    config: HarnessConfig,
    wdir: str,
    env: dict,
    host: CodeHost,
    repo: RepositoryRef,
    current_user: str,
    dry_run: bool,
) -> None:
    """Process one PR. host read/write failures are logged, not returned; git errors raise."""
    pr_number = pr.number
    logger.info("PR #%d: starting%s", pr_number, " (dry-run)" if dry_run else "")
    original_sha = await git_detach_and_record(wdir, env)
    try:
        checked_out_sha = await _checkout_pr_head(pr_number, wdir, env)
        size = await evaluate_size(pr.base_reference, wdir, env, config.auto_approve)
        await _decide_and_act(pr, size, checked_out_sha, config, host, repo, current_user, dry_run)
    finally:
        # Detached checkout: no local branch was created, so there is none to delete.
        await git_restore(original_sha, "", wdir, env)


async def _checkout_pr_head(pr_number: int, wdir: str, env: dict) -> str:
    """Detach at the PR's real head (``refs/pull/<n>/head``) and return its SHA.

    Unlike the remote head branch, this is the commit GitHub reviews, also for fork PRs, so the
    size measured here is the size of the commit that gets approved.
    """
    try:
        await run_cmd(["git", "fetch", "origin", f"refs/pull/{pr_number}/head"], cwd=wdir, env=env, timeout=TIMEOUT_GIT)
        await run_cmd(
            ["git", "checkout", "--recurse-submodules", "--detach", "FETCH_HEAD"],
            cwd=wdir,
            env=env,
            timeout=TIMEOUT_GIT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise FatalGitError(f"git checkout of PR #{pr_number} head failed: {e}") from e  # noqa: TRY003
    return await get_head_sha(wdir, env)


def _log_size(pr_number: int, size: SizeEvaluation, config: HarnessConfig) -> None:
    logger.info(
        "PR #%d: counted_files=%d total_lines=%d small_files=%d pr_is_small=%s viewed_max_lines=%d approve_max_lines=%d",
        pr_number,
        len(size.file_counts),
        size.total_lines,
        len(size.small_files),
        size.pr_is_small,
        config.auto_approve.viewed_max_lines,
        config.auto_approve.approve_max_lines,
    )


async def _decide_and_act(
    pr: ChangeRequest,
    size: SizeEvaluation,
    checked_out_sha: str,
    config: HarnessConfig,
    host: CodeHost,
    repo: RepositoryRef,
    current_user: str,
    dry_run: bool,
) -> None:
    pr_number = pr.number
    _log_size(pr_number, size, config)

    # All reads first (NFR-1): any read failure means no write on this PR this run.
    author = pr.author.login if pr.author else ""
    gate = await check_timing_gate(host, repo, pr_number, author, current_user)
    logger.info("PR #%d: timing gate %s (%s)", pr_number, gate.status.value, gate.reason)
    safety = await check_approval_safety(host, repo, pr_number, current_user)
    logger.info("PR #%d: safety %s (%s) head=%s", pr_number, safety.status.value, safety.reason, safety.head_sha)
    if gate.status is GateStatus.READ_FAILED or safety.status is SafetyStatus.READ_FAILED:
        logger.error("PR #%d: a read failed, no writes this run; retrying next run", pr_number)
        return

    if safety.head_sha != checked_out_sha:
        logger.warning(
            "PR #%d: head moved since size was measured (measured %s, now %s), no writes this run",
            pr_number,
            checked_out_sha,
            safety.head_sha,
        )
        return

    gate_passed = gate.status is GateStatus.PASSED
    viewed = await _mark_viewed(host, repo, pr_number, size, config, gate_passed, dry_run)
    if viewed is None:
        logger.error("PR #%d: viewed-state read failed, no writes this run; retrying next run", pr_number)
        return
    all_viewed = viewed
    if not size.file_counts and not all_viewed:
        logger.info("PR #%d: no counted files (only renames/deletes/binary), not approving", pr_number)
        return
    if all_viewed:
        logger.info("PR #%d: every file is viewed, size limit waived for approval", pr_number)
    approval = await approve_pr(
        host,
        repo,
        pr_number,
        gate,
        safety,
        size,
        config.auto_approve.approve_max_lines,
        current_user,
        dry_run,
        all_viewed=all_viewed,
    )
    level = logging.ERROR if approval.status is ApprovalStatus.WRITE_FAILED else logging.INFO
    logger.log(level, "PR #%d: approval %s (%s)", pr_number, approval.status.value, approval.reason)


async def _mark_viewed(
    host: CodeHost,
    repo: RepositoryRef,
    pr_number: int,
    size: SizeEvaluation,
    config: HarnessConfig,
    gate_passed: bool,
    dry_run: bool,
) -> bool | None:
    """Mark small files viewed. Returns whether all files are viewed, or None when the read failed.

    Runs even when no file is small: the viewed states are also how a PR the reviewer viewed by
    hand is recognised.
    """
    max_lines = config.auto_approve.viewed_max_lines
    viewed = await mark_files_viewed(host, repo, pr_number, size.file_counts, max_lines, gate_passed, dry_run=dry_run)
    logger.info(
        "PR #%d: viewed %s marked=%s already-viewed=%s failed=%s",
        pr_number,
        viewed.status.value,
        viewed.marked,
        viewed.skipped,
        viewed.failed,
    )
    if viewed.status is ViewedStatus.READ_FAILED:
        return None
    return viewed.all_viewed
