"""Merge runner: squash-merges the acting user's own PRs once every merge condition holds.

Public entry point (takes the shared working-directory lock itself, like the sibling runners)::

    run(config: HarnessConfig, *, dry_run: bool = False, pr: int | None = None) -> None

* ``config.merge.enabled`` false: returns immediately, no lock, no host call.
* Otherwise processes every open PR authored by the acting user (or only ``pr``).
* A PR is merged only when every condition holds (checked in this order, first failure wins, one
  skip reason, no merge call): author is the acting user, open/non-draft/no conflict, required
  checks pass, the gate record matches the head SHA, changed lines <= ``max_changed_lines``, and
  (``require_all_files_viewed``) every file is viewed. Viewed state is read fresh on every run.
* The only write is ``ChangeRequests.squash_merge(expected_head=<sha>)``. Never approves, never merges
  with admin rights, auto-merge or branch deletion. ``dry_run=True`` logs "would merge" instead. A timeout of the
  merge call may have taken effect: it is never retried and is collected as a failure.
* A skip is logged as ``PR #N: skipped: <reason>`` and is not an error. A per-PR exception (or a
  failed merge call, a ``CodeHostError`` wrapped in :class:`MergeError`) is collected and raised as :class:`MergeError` after the loop.

Gate record: the evidence that ``build.commands`` passed on the head SHA is written by Package A
(the sisyphus gate-record writer). The format is settled: a JSON file
``{"head_sha": "<sha>"}`` (other keys are ignored) at ``<XDG_DATA>/state/<repo_slug>/build_gate_<pr>.json``
(see :func:`gate_record_path`). The reader here and the gate-record writer must stay in sync. A
missing, unreadable or SHA-mismatched record blocks the merge.
"""

import json
import logging
from pathlib import Path

from dotharness.code_host import (
    ChangeRequest,
    CodeHost,
    CodeHostError,
    RepositoryRef,
    UnsupportedCapabilityError,
)
from dotharness.pr_review import state
from dotharness.pr_review.config import HarnessConfig
from dotharness.pr_review.lock import acquire_lock
from dotharness.pr_review.runners.ci_checks import (
    change_request_skip_reason,
    ci_blocker,
    guarded_read,
    list_own_open_change_requests,
    read_change_request,
)
from dotharness.pr_review.runners.common import call_with_policy, host_current_user, open_host

logger = logging.getLogger(__name__)

_SITE = "merge"

SKIP_NOT_AUTHOR = "not_author"
SKIP_STATE_READ_FAILED = "state_read_failed"
SKIP_CHECKS_READ_FAILED = "checks_read_failed"
SKIP_GATE_RECORD_MISSING = "gate_record_missing"
SKIP_GATE_RECORD_STALE = "gate_record_stale"
SKIP_SIZE_READ_FAILED = "size_read_failed"
SKIP_OVER_MAX_CHANGED_LINES = "over_max_changed_lines"
SKIP_UNVIEWED_FILES = "unviewed_files"
SKIP_VIEWED_READ_FAILED = "viewed_read_failed"

# Reasons from ``change_request_skip_reason`` and ``ci_blocker`` (ci_checks): "not-open", "draft",
# "conflicting", "mergeable-unknown", "auto-merge", "ci-blocked".
MERGED = "merged"
WOULD_MERGE = "would-merge"


class MergeError(RuntimeError):
    """Raised by run() after the loop when one or more PRs failed."""


async def run(config: HarnessConfig, *, dry_run: bool = False, pr: int | None = None) -> None:
    if not config.merge.enabled:
        logger.info("merge is disabled, nothing to do")
        return
    with acquire_lock(config.lock_key):
        await _run_locked(config, dry_run, pr)


async def _run_locked(config: HarnessConfig, dry_run: bool, pr: int | None) -> None:
    scaffold = await open_host(config)
    host, repo = scaffold.host, scaffold.repo
    current_user = await host_current_user(host)
    failures: list[str] = []
    prs = await list_own_open_change_requests(host, repo, current_user, pr, failures, _SITE)
    summary: dict[int, str] = {}
    for item in prs:
        number = item.number
        try:
            summary[number] = await _process_pr(item, config, host, repo, current_user, dry_run)
        except UnsupportedCapabilityError:
            raise
        except Exception as e:
            logger.exception("PR #%d: error", number)
            failures.append(f"#{number}: {e}")
    logger.info("merge summary: %s", summary or "no PRs")
    if failures:
        raise MergeError("merge failed for: " + "; ".join(failures))


def gate_record_path(repo_slug: str, pr_number: int) -> Path:
    """Location of the gate record written by Package A (sisyphus gaterecord.py). Read-only: creates no directory."""
    return state.XDG_DATA / "state" / state._validate_repo_slug(repo_slug) / f"build_gate_{pr_number}.json"


def read_gate_record(repo_slug: str, pr_number: int) -> str | None:
    """Return the head SHA the record vouches for, or None when it is missing or unreadable."""
    try:
        data = json.loads(gate_record_path(repo_slug, pr_number).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = data.get("head_sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


async def _read_changed_lines(host: CodeHost, repo: RepositoryRef, number: int) -> int | None:
    change_request = await read_change_request(host, repo, number, _SITE)
    if change_request is None or change_request.additions is None or change_request.deletions is None:
        return None
    return change_request.additions + change_request.deletions


async def _evaluate(
    pr: ChangeRequest, config: HarnessConfig, host: CodeHost, repo: RepositoryRef, current_user: str
) -> tuple[str | None, str]:
    """Return (skip reason or None, evaluated head revision). Stops at the first failed condition."""
    number = pr.number
    author = pr.author.login if pr.author else ""
    if not current_user or author != current_user:
        return SKIP_NOT_AUTHOR, ""
    fresh = await read_change_request(host, repo, number, _SITE)
    if fresh is None:
        return SKIP_STATE_READ_FAILED, ""
    head_sha = fresh.head_revision
    reason = change_request_skip_reason(fresh)
    if reason is None:
        reason = await _check_ci_and_record(config, host, repo, number, head_sha, fresh.base_reference)
    if reason is None:
        reason = await _check_size_and_viewed(config, host, repo, number)
    return reason, head_sha


async def _check_ci_and_record(
    config: HarnessConfig, host: CodeHost, repo: RepositoryRef, number: int, head_sha: str, base_branch: str
) -> str | None:
    ci = await ci_blocker(host, repo, number, base_branch)
    if ci is not None:
        return SKIP_CHECKS_READ_FAILED if ci.read_failed else ci.reason
    recorded = read_gate_record(config.repo_slug, number)
    if recorded is None:
        return SKIP_GATE_RECORD_MISSING
    if recorded != head_sha:
        return SKIP_GATE_RECORD_STALE
    return None


async def _check_size_and_viewed(config: HarnessConfig, host: CodeHost, repo: RepositoryRef, number: int) -> str | None:
    lines = await _read_changed_lines(host, repo, number)
    if lines is None:
        return SKIP_SIZE_READ_FAILED
    if lines > config.merge.max_changed_lines:
        return SKIP_OVER_MAX_CHANGED_LINES
    if config.merge.require_all_files_viewed:
        viewed = await guarded_read(  # always fresh, never cached
            _SITE,
            "ChangeRequests.viewed_files",
            lambda: host.change_requests.viewed_files(repo, number),
            f"PR #{number}: reading viewed files",
        )
        if viewed is None:
            return SKIP_VIEWED_READ_FAILED
        if not viewed or not all(viewed.values()):
            return SKIP_UNVIEWED_FILES
    return None


async def _process_pr(
    pr: ChangeRequest, config: HarnessConfig, host: CodeHost, repo: RepositoryRef, current_user: str, dry_run: bool
) -> str:
    """Process one PR; return its outcome (a skip reason, MERGED or WOULD_MERGE)."""
    number = pr.number
    reason, head_sha = await _evaluate(pr, config, host, repo, current_user)
    if reason:
        logger.info("PR #%d: skipped: %s", number, reason)
        return reason
    if dry_run:
        logger.info("PR #%d: would merge (dry-run) at %s", number, head_sha)
        return WOULD_MERGE
    try:
        await call_with_policy(
            _SITE,
            "ChangeRequests.squash_merge",
            lambda: host.change_requests.squash_merge(repo, number, expected_head=head_sha),
            None,
        )
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as e:
        # A timeout may have taken effect: the error carries that text; the merge is never retried here.
        raise MergeError(f"merge of #{number} failed: {e}") from e  # noqa: TRY003
    logger.info("PR #%d: merged at %s", number, head_sha)
    return MERGED
