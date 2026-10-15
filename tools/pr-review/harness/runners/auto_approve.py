"""Auto-approve runner: marks small files viewed and approves small PRs (issue #95).

Public entry point (takes the shared working-directory lock itself, like the sibling runners)::

    run(config: HarnessConfig, *, dry_run: bool = False) -> None

* ``config.auto_approve.enabled`` false: returns immediately, no gh/git call at all.
* Otherwise processes every open, non-draft PR in ``user-review-requested:@me``; each PR is
  isolated (a failure is logged and the loop continues).
* ``dry_run=True`` performs every read and check but no write: the sibling write functions
  in ``auto_approve_github`` (the only write points: ``markFileAsViewed`` and the review POST)
  receive ``dry_run`` and log what they would do.
* Raises :class:`AutoApproveError` after the loop if any PR failed (read failure, write failure,
  or an exception), so the CLI exits non-zero. Returns None when every PR succeeded.

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

import json
import logging
import subprocess

from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.runners.auto_approve_github import (
    ApprovalStatus,
    GateStatus,
    SafetyStatus,
    ViewedStatus,
    approve_pr,
    check_approval_safety,
    check_timing_gate,
    mark_files_viewed,
)
from harness.runners.auto_approve_size import SizeEvaluation, evaluate_size
from harness.runners.common import (
    TIMEOUT_GH,
    TIMEOUT_GIT,
    FatalGitError,
    build_subprocess_env,
    get_current_user,
    get_gh_token,
    get_head_sha,
    git_detach_and_record,
    git_restore,
    run_cmd,
)

logger = logging.getLogger(__name__)


class AutoApproveError(RuntimeError):
    """Raised by run() after the loop when one or more PRs failed."""


def run(config: HarnessConfig, *, dry_run: bool = False) -> None:
    with acquire_lock(config.lock_key):
        _run_locked(config, dry_run)


def _run_locked(config: HarnessConfig, dry_run: bool) -> None:
    if not config.auto_approve.enabled:
        logger.info("auto-approve is disabled, nothing to do")
        return
    gh_token = get_gh_token(config.harness.gh_token_cmd)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env, gh_token)
    current_user = get_current_user(env)
    prs = _list_prs(config.repo.name, env)
    wdir = str(config.repo.working_dir)
    if not prs:
        logger.info(
            "auto-approve: no open PRs with a pending review request for %s on %s, nothing to do",
            current_user,
            config.repo.name,
        )

    failures: list[str] = []
    for pr in prs:
        if not _is_candidate(pr):
            continue
        pr_number = pr["number"]
        try:
            problems = _process_pr(pr, config, wdir, env, current_user, dry_run)
        except FatalGitError as e:
            logger.exception("PR #%d: fatal git error", pr_number)
            failures.append(f"#{pr_number}: {e}")
            break
        except Exception as e:
            logger.exception("PR #%d: error", pr_number)
            failures.append(f"#{pr_number}: {e}")
            continue
        failures.extend(f"#{pr_number}: {p}" for p in problems)
    if failures:
        raise AutoApproveError("auto-approve failed for: " + "; ".join(failures))


def _list_prs(repo: str, env: dict) -> list[dict]:
    result = run_cmd(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            "user-review-requested:@me",
            "--json",
            "number,headRefName,baseRefName,isDraft,isCrossRepository,state,author",
            "--limit",
            "500",
        ],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        raise AutoApproveError(f"gh pr list failed (exit {result.returncode})")  # noqa: TRY003
    return sorted(json.loads(result.stdout), key=lambda p: p["number"])


def _is_candidate(pr: dict) -> bool:
    if pr.get("isDraft"):
        logger.info("PR #%d: draft, skipping", pr["number"])
        return False
    if pr.get("state", "OPEN") != "OPEN":
        logger.info("PR #%d: not open (%s), skipping", pr["number"], pr.get("state"))
        return False
    if pr.get("isCrossRepository"):
        logger.info("PR #%d: from a fork, skipping", pr["number"])
        return False
    if not pr.get("headRefName") or not pr.get("baseRefName"):
        logger.info("PR #%d: missing head/base branch, skipping", pr["number"])
        return False
    return True


def _process_pr(pr: dict, config: HarnessConfig, wdir: str, env: dict, current_user: str, dry_run: bool) -> list[str]:
    """Process one PR; return a list of problems (empty when it succeeded)."""
    pr_number = pr["number"]
    logger.info("PR #%d: starting%s", pr_number, " (dry-run)" if dry_run else "")
    original_sha = git_detach_and_record(wdir, env)
    try:
        checked_out_sha = _checkout_pr_head(pr_number, wdir, env)
        size = evaluate_size(pr["baseRefName"], wdir, env, config.auto_approve)
        return _decide_and_act(pr, size, checked_out_sha, config, env, current_user, dry_run)
    finally:
        # Detached checkout: no local branch was created, so there is none to delete.
        git_restore(original_sha, "", wdir, env)


def _checkout_pr_head(pr_number: int, wdir: str, env: dict) -> str:
    """Detach at the PR's real head (``refs/pull/<n>/head``) and return its SHA.

    Unlike ``origin/<headRefName>``, this is the commit GitHub reviews, also for fork PRs, so the
    size measured here is the size of the commit that gets approved.
    """
    try:
        run_cmd(["git", "fetch", "origin", f"refs/pull/{pr_number}/head"], cwd=wdir, env=env, timeout=TIMEOUT_GIT)
        run_cmd(
            ["git", "checkout", "--recurse-submodules", "--detach", "FETCH_HEAD"],
            cwd=wdir,
            env=env,
            timeout=TIMEOUT_GIT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise FatalGitError(f"git checkout of PR #{pr_number} head failed: {e}") from e  # noqa: TRY003
    return get_head_sha(wdir, env)


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


def _decide_and_act(
    pr: dict,
    size: SizeEvaluation,
    checked_out_sha: str,
    config: HarnessConfig,
    env: dict,
    current_user: str,
    dry_run: bool,
) -> list[str]:
    pr_number = pr["number"]
    repo = config.repo.name
    _log_size(pr_number, size, config)

    # All reads first (NFR-1): any read failure means no write on this PR this run.
    author = (pr.get("author") or {}).get("login", "")
    gate = check_timing_gate(repo, pr_number, author, current_user, env)
    logger.info("PR #%d: timing gate %s (%s)", pr_number, gate.status.value, gate.reason)
    safety = check_approval_safety(repo, pr_number, current_user, env)
    logger.info("PR #%d: safety %s (%s) head=%s", pr_number, safety.status.value, safety.reason, safety.head_sha)
    if gate.status is GateStatus.READ_FAILED or safety.status is SafetyStatus.READ_FAILED:
        logger.error("PR #%d: a read failed, no writes this run", pr_number)
        return ["read failed (timing gate or safety checks), no writes"]

    if safety.head_sha != checked_out_sha:
        logger.warning(
            "PR #%d: head moved since size was measured (measured %s, now %s), no writes this run",
            pr_number,
            checked_out_sha,
            safety.head_sha,
        )
        return []

    gate_passed = gate.status is GateStatus.PASSED
    viewed = _mark_viewed(pr_number, size, config, gate_passed, env, dry_run)
    if viewed is None:
        return ["viewed-state read failed, no writes"]
    problems, all_viewed = viewed
    if not size.file_counts and not all_viewed:
        logger.info("PR #%d: no counted files (only renames/deletes/binary), not approving", pr_number)
        return problems
    if all_viewed:
        logger.info("PR #%d: every file is viewed, size limit waived for approval", pr_number)
    approval = approve_pr(
        repo,
        pr_number,
        gate,
        safety,
        size,
        config.auto_approve.approve_max_lines,
        current_user,
        env,
        dry_run,
        all_viewed=all_viewed,
    )
    logger.info("PR #%d: approval %s (%s)", pr_number, approval.status.value, approval.reason)
    if approval.status in (ApprovalStatus.READ_FAILED, ApprovalStatus.WRITE_FAILED):
        problems.append(f"approval {approval.status.value}: {approval.reason}")
    return problems


def _mark_viewed(
    pr_number: int, size: SizeEvaluation, config: HarnessConfig, gate_passed: bool, env: dict, dry_run: bool
) -> tuple[list[str], bool] | None:
    """Mark small files viewed. Returns (problems, all files viewed), or None when the read failed.

    Runs even when no file is small: the viewed states are also how a PR the reviewer viewed by
    hand is recognised.
    """
    max_lines = config.auto_approve.viewed_max_lines
    viewed = mark_files_viewed(
        config.repo.name, pr_number, size.file_counts, max_lines, gate_passed, env, dry_run=dry_run
    )
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
    return [f"failed to mark viewed: {p}" for p in viewed.failed], viewed.all_viewed
