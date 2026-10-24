"""Merge runner: squash-merges the acting user's own PRs once every merge condition holds.

Public entry point (takes the shared working-directory lock itself, like the sibling runners)::

    run(config: HarnessConfig, *, dry_run: bool = False, pr: int | None = None) -> None

* ``config.merge.enabled`` false: returns immediately, no lock, no gh call.
* Otherwise processes every open PR authored by the acting user (or only ``pr``).
* A PR is merged only when every condition holds (checked in this order, first failure wins, one
  skip reason, no merge call): author is the acting user, open/non-draft/no conflict, required
  checks pass, the gate record matches the head SHA, changed lines <= ``max_changed_lines``, and
  (``require_all_files_viewed``) every file is viewed. Viewed state is read fresh on every run.
* The only write is ``gh pr merge N --repo R --squash --match-head-commit <sha>``. Never approves,
  never ``--admin``/``--auto``/``--delete-branch``. ``dry_run=True`` logs "would merge" instead.
* A skip is logged as ``PR #N: skipped: <reason>`` and is not an error. A per-PR exception (or a
  failed merge call) is collected and raised as :class:`MergeError` after the loop.

Gate record: the evidence that ``build.commands`` passed on the head SHA is written by Package A
(``tools/sisyphus/sisyphus/gaterecord.py``). The format is settled: a JSON file
``{"head_sha": "<sha>"}`` (other keys are ignored) at ``<XDG_DATA>/state/<repo_slug>/build_gate_<pr>.json``
(see :func:`gate_record_path`). The reader here and the writer in gaterecord.py must stay in sync. A
missing, unreadable or SHA-mismatched record blocks the merge.
"""

import json
import logging
from pathlib import Path

from harness import state
from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.runners.auto_approve_github import (
    VIEWED_STATE,
    SafetyStatus,
    _check_ci,
    _gh,
    _read_pr_state,
    _state_skip_reason,
    _text,
    list_own_open_prs,
    read_viewed_states,
)
from harness.runners.common import build_subprocess_env, get_current_user, get_gh_token

logger = logging.getLogger(__name__)

SKIP_NOT_AUTHOR = "not_author"
SKIP_STATE_READ_FAILED = "state_read_failed"
SKIP_CHECKS_READ_FAILED = "checks_read_failed"
SKIP_GATE_RECORD_MISSING = "gate_record_missing"
SKIP_GATE_RECORD_STALE = "gate_record_stale"
SKIP_SIZE_READ_FAILED = "size_read_failed"
SKIP_OVER_MAX_CHANGED_LINES = "over_max_changed_lines"
SKIP_UNVIEWED_FILES = "unviewed_files"
SKIP_VIEWED_READ_FAILED = "viewed_read_failed"

# Reasons from _state_skip_reason (auto_approve_github) and _check_ci: "not-open", "draft",
# "conflicting", "mergeable-unknown", "auto-merge", "ci-blocked".
MERGED = "merged"
WOULD_MERGE = "would-merge"


class MergeError(RuntimeError):
    """Raised by run() after the loop when one or more PRs failed."""


def run(config: HarnessConfig, *, dry_run: bool = False, pr: int | None = None) -> None:
    if not config.merge.enabled:
        logger.info("merge is disabled, nothing to do")
        return
    with acquire_lock(config.lock_key):
        _run_locked(config, dry_run, pr)


def _run_locked(config: HarnessConfig, dry_run: bool, pr: int | None) -> None:
    gh_token = get_gh_token(config.harness.gh_token_cmd)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env, gh_token)
    current_user = get_current_user(env)
    repo = config.repo.name
    failures: list[str] = []
    prs = list_own_open_prs(repo, pr, env, failures, "number,author", _gh)
    summary: dict[int, str] = {}
    for item in sorted(prs, key=lambda p: p["number"]):
        number = item["number"]
        try:
            summary[number] = _process_pr(item, config, env, current_user, dry_run)
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


def _read_changed_lines(repo: str, number: int, env: dict) -> int | None:
    result = _gh(["pr", "view", str(number), "--repo", repo, "--json", "additions,deletions"], env)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(_text(result.stdout))
        return int(data["additions"]) + int(data["deletions"])
    except (ValueError, KeyError, TypeError):
        return None


def _evaluate(pr: dict, config: HarnessConfig, env: dict, current_user: str) -> tuple[str | None, str]:
    """Return (skip reason or None, evaluated head SHA). Stops at the first failed condition."""
    number = pr["number"]
    repo = config.repo.name
    author = (pr.get("author") or {}).get("login", "")
    if not current_user or author != current_user:
        return SKIP_NOT_AUTHOR, ""
    pr_state = _read_pr_state(repo, number, env)
    if pr_state is None:
        return SKIP_STATE_READ_FAILED, ""
    head_sha = pr_state["headRefOid"]
    reason = _state_skip_reason(pr_state)
    if reason is None:
        reason = _check_ci_and_record(config, number, head_sha, pr_state.get("baseRefName", ""), env)
    if reason is None:
        reason = _check_size_and_viewed(config, number, env)
    return reason, head_sha


def _check_ci_and_record(config: HarnessConfig, number: int, head_sha: str, base_branch: str, env: dict) -> str | None:
    ci = _check_ci(config.repo.name, number, base_branch, env)
    if ci is not None:
        return SKIP_CHECKS_READ_FAILED if ci.status is SafetyStatus.READ_FAILED else ci.reason
    recorded = read_gate_record(config.repo_slug, number)
    if recorded is None:
        return SKIP_GATE_RECORD_MISSING
    if recorded != head_sha:
        return SKIP_GATE_RECORD_STALE
    return None


def _check_size_and_viewed(config: HarnessConfig, number: int, env: dict) -> str | None:
    repo = config.repo.name
    lines = _read_changed_lines(repo, number, env)
    if lines is None:
        return SKIP_SIZE_READ_FAILED
    if lines > config.merge.max_changed_lines:
        return SKIP_OVER_MAX_CHANGED_LINES
    if config.merge.require_all_files_viewed:
        viewed = read_viewed_states(repo, number, env)  # always fresh, never cached
        if viewed is None:
            return SKIP_VIEWED_READ_FAILED
        states = viewed[1]
        if not states or any(s != VIEWED_STATE for s in states.values()):
            return SKIP_UNVIEWED_FILES
    return None


def _process_pr(pr: dict, config: HarnessConfig, env: dict, current_user: str, dry_run: bool) -> str:
    """Process one PR; return its outcome (a skip reason, MERGED or WOULD_MERGE)."""
    number = pr["number"]
    reason, head_sha = _evaluate(pr, config, env, current_user)
    if reason:
        logger.info("PR #%d: skipped: %s", number, reason)
        return reason
    if dry_run:
        logger.info("PR #%d: would merge (dry-run) at %s", number, head_sha)
        return WOULD_MERGE
    result = _gh(
        ["pr", "merge", str(number), "--repo", config.repo.name, "--squash", "--match-head-commit", head_sha],
        env,
    )
    if result.returncode != 0:
        raise MergeError(f"gh pr merge failed (exit {result.returncode}): {_text(result.stderr).strip()}")  # noqa: TRY003
    logger.info("PR #%d: merged at %s", number, head_sha)
    return MERGED
