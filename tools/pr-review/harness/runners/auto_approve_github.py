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
OD-3 any bucket other than pass/skipping blocks with the single reason "CI blocked", judged on the
latest run of each check only. Runs are keyed by (``workflow``, ``name``); among several runs of one
key the latest by ``completedAt`` (else ``startedAt``) decides, so a failed run followed by a passing
rerun does not block (see ``latest_runs``). A PR with no checks passes only when the repo has no active
workflow (empty is also what gh reports before a push's workflows register).
Stack-root rule (Phase 3b): when the PR's own base has no required checks (a stacked PR's base is another
PR's unprotected head branch), the base chain is walked through open PRs matched by head branch name only
(``_find_stack_root``) up to the default branch. Only the check names required at the root PR are then
evaluated on this PR; a required name with no run on it is not passing, and an empty required set with no
checks passes. With no stack, or a root with no required checks, all checks are evaluated as before. OD-A: more
than one open PR for one head name is ambiguous and fails closed (READ_FAILED). A walk deeper than
``_MAX_STACK_DEPTH``, any read failure, malformed output, or pending required checks at the root also fail
closed. A PR whose own base has required checks makes no walk calls.

FR-6.4: approval idempotency uses a new tri-state read (``_read_approval_state``) instead of
``review_requested._has_user_approved`` (which returns False on a read error); a read failure
never leads to a write. As in the safety check (shared ``_already_approved``), any APPROVED
review by the acting account, regardless of commit_id, counts as "already approved". The
head-moved guard and the ``commit_id`` binding on the POST are kept.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import NamedTuple

from harness.runners.auto_approve_size import SizeEvaluation
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


def _scan_issue_comments(issue_comments: list[dict], acting: str) -> tuple[bool, bool, bool, list[datetime]] | str:
    has_summary = has_design = has_traceability = False
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
    return has_summary, has_design, has_traceability, qualifying


def _is_review_inline_comment(body: str) -> bool:
    """An inline comment from any review pass: the design and traceability passes tag theirs with
    their own marker, not the file pass's inline marker."""
    return is_inline_review_comment(body) or is_design_review_comment(body) or is_traceability_review_comment(body)


def _scan_inline_comments(review_comments: list[dict], acting: str) -> list[datetime] | str:
    qualifying: list[datetime] = []
    for comment in review_comments:
        if _login(comment) == acting and _is_review_inline_comment(comment.get("body") or ""):
            ts = _parse_ts(comment.get("created_at"))
            if ts is None:
                return "inline comment has no valid created_at"
            qualifying.append(ts)
    return qualifying


def _scan_own_comments(
    issue_comments: list[dict], review_comments: list[dict], acting: str
) -> tuple[bool, bool, bool, int, list[datetime]] | str:
    """Scan the acting account's review comments.

    Returns (has_summary, has_design, has_traceability, inline_count, qualifying timestamps),
    or a failure reason string when a qualifying comment has no valid created_at.
    """
    issues = _scan_issue_comments(issue_comments, acting)
    if isinstance(issues, str):
        return issues
    inline = _scan_inline_comments(review_comments, acting)
    if isinstance(inline, str):
        return inline
    has_summary, has_design, has_traceability, qualifying = issues
    return has_summary, has_design, has_traceability, len(inline), qualifying + inline


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


class SafetyStatus(Enum):
    SAFE = "safe"
    SKIP = "skip"
    READ_FAILED = "read_failed"


REASON_DRAFT = "draft"
REASON_NOT_OPEN = "not-open"
REASON_CONFLICTING = "conflicting"
REASON_MERGEABLE_UNKNOWN = "mergeable-unknown"
REASON_AUTO_MERGE = "auto-merge"
REASON_CI_BLOCKED = "ci-blocked"
REASON_ALREADY_APPROVED = "already-approved"

_NO_REQUIRED_CHECKS_TEXT = "no required checks reported"
_NO_CHECKS_TEXT = "no checks reported"
_PASSING_BUCKETS = frozenset({"pass", "skipping"})
_MAX_NAMED_CHECKS = 10
_UNNAMED_CHECK = "<unnamed>"
_MAX_STACK_DEPTH = 10


class Check(NamedTuple):
    name: str
    bucket: str


@dataclass(frozen=True)
class SafetyResult:
    status: SafetyStatus
    reason: str
    head_sha: str = ""


def _gh(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return run_cmd(["gh", *args], cwd="/", env=env, timeout=TIMEOUT_GH, check=False)


def _text(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def list_own_open_prs(
    repo: str, only_pr: int | None, env: dict, failures: list[str], fields: str, gh: Callable = _gh
) -> list[dict]:
    """Open PRs authored by the acting user (``gh``'s ``@me``), or just ``only_pr``, sorted by number.

    Any problem (gh failure, non-JSON output, not a list of PR objects) is appended to ``failures``
    and yields ``[]``. ``gh`` lets a caller pass its own module's ``_gh``.
    """
    if only_pr is None:
        args = ["pr", "list", "--repo", repo, "--state", "open", "--author", "@me", "--json", fields]
        args += ["--limit", "500"]
    else:
        args = ["pr", "view", str(only_pr), "--repo", repo, "--json", fields]
    result = gh(args, env)
    if result.returncode != 0:
        failures.append(f"gh pr {'list' if only_pr is None else 'view'} failed (exit {result.returncode})")
        return []
    try:
        data = json.loads(_text(result.stdout))
    except ValueError:
        failures.append("gh pr output was not JSON")
        return []
    prs = data if isinstance(data, list) else [data]
    if not all(isinstance(p, dict) and isinstance(p.get("number"), int) for p in prs):
        failures.append("gh pr output was not a list of PR objects")
        return []
    return sorted(prs, key=lambda p: p["number"])


_ZERO_YEAR = 1  # gh reports an unset completedAt as 0001-01-01T00:00:00Z


def _check_ts(value: object) -> datetime | None:
    """A usable check timestamp (aware, not the zero value), else None."""
    ts = _parse_ts(value)
    if ts is None or ts.year <= _ZERO_YEAR:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _run_key(entry: dict) -> tuple[str, str]:
    workflow, name = entry.get("workflow"), entry.get("name")
    return (workflow if isinstance(workflow, str) else "", name if isinstance(name, str) else "")


def _newest_runs(runs: list[dict]) -> list[dict] | None:
    """The latest run(s) of one check key; None when a run has no usable timestamp."""
    if len(runs) == 1:
        return runs
    stamped = []
    for run in runs:
        completed = _check_ts(run.get("completedAt"))
        when = completed or _check_ts(run.get("startedAt"))
        if when is None:
            return None
        stamped.append((when, completed is not None, run))
    newest = max(when for when, _, _ in stamped)
    return [run if done else {**run, "bucket": "pending"} for when, done, run in stamped if when == newest]


def latest_runs(entries: list[dict]) -> list[dict] | None:
    """Keep only the latest run of each check (key = ``workflow`` + ``name``); None = cannot decide (fail closed).

    A key with one run is kept as is and needs no timestamps. For a key with several runs, "latest" is the
    greatest ``completedAt``, falling back to ``startedAt`` when ``completedAt`` is missing, empty or the zero
    value; an incomplete run (``startedAt`` only) is placed on the same timeline by its ``startedAt``. A run with
    neither usable timestamp makes the result None. If the latest run has no ``completedAt`` it is not completed
    and counts as pending whatever its bucket says. Runs tied for latest are all kept, so a tie blocks when any
    of them blocks. Entries come back in first-seen key order. gh's real values for pending, queued and skipped
    runs are unverified; behaviour is pinned by fixtures only.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        groups.setdefault(_run_key(entry), []).append(entry)
    kept: list[dict] = []
    for runs in groups.values():
        newest = _newest_runs(runs)
        if newest is None:
            return None
        kept.extend(newest)
    return kept


def _parse_buckets(stdout: str) -> list[Check] | None:
    """Return the latest run of every check (name, bucket), or None when the output is unusable.

    ``bucket`` is required; a missing or non-string ``name`` is tolerated and shown as ``<unnamed>``.
    Superseded runs are dropped by ``latest_runs``; None also when that cannot decide.
    """
    try:
        checks = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(checks, list):
        return None
    if not all(isinstance(c, dict) and isinstance(c.get("bucket"), str) for c in checks):
        return None
    latest = latest_runs(checks)
    if latest is None:
        return None
    parsed = []
    for check in latest:
        name = check.get("name")
        parsed.append(Check(name if isinstance(name, str) and name else _UNNAMED_CHECK, check["bucket"]))
    return parsed


def _read_buckets(repo: str, pr_number: int, env: dict, required: bool) -> list[Check] | None:
    """Latest run of each check (name, bucket) for the PR. None = read failure; [] with required=True = no required checks."""
    args = ["pr", "checks", str(pr_number), "--repo", repo, "--json", "name,bucket,workflow,startedAt,completedAt"]
    result = _gh([*args, "--required"] if required else args, env)
    stderr = _text(result.stderr)
    if required and result.returncode == 1 and _NO_REQUIRED_CHECKS_TEXT in stderr:
        return []
    if not required and result.returncode == 1 and _NO_CHECKS_TEXT in stderr:
        return []
    if result.returncode == 8 and not _text(result.stdout).strip():
        return None  # pending exit without JSON: cannot decide from buckets
    if result.returncode not in (0, 8):
        logger.error("PR #%d: gh pr checks failed (exit %d): %s", pr_number, result.returncode, stderr.strip())
        return None
    return _parse_buckets(_text(result.stdout))


def _read_default_branch(repo: str, env: dict) -> str | None:
    """The repository default branch (``gh repo view --json defaultBranchRef``), or None on any failure.

    Failure means a non-zero exit, a timeout, unparseable JSON, or a missing or empty ``defaultBranchRef.name``.
    """
    try:
        result = _gh(["repo", "view", repo, "--json", "defaultBranchRef"], env)
    except subprocess.TimeoutExpired:
        logger.exception("gh repo view timed out for %s", repo)
        return None
    if result.returncode != 0:
        logger.error("gh repo view failed for %s (exit %d): %s", repo, result.returncode, _text(result.stderr).strip())
        return None
    try:
        name = json.loads(_text(result.stdout))["defaultBranchRef"]["name"]
    except (ValueError, KeyError, TypeError):
        return None
    return name if isinstance(name, str) and name else None


def _open_prs_with_head(repo: str, branch: str, env: dict) -> list[dict] | None:
    """Open PRs whose head branch is ``branch`` (name only, D-5), as ``{number, baseRefName}``; None on failure."""
    args = ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,baseRefName"]
    try:
        result = _gh(args, env)
    except subprocess.TimeoutExpired:
        logger.exception("gh pr list timed out for head %s", branch)
        return None
    if result.returncode != 0:
        logger.error(
            "gh pr list --head %s failed (exit %d): %s", branch, result.returncode, _text(result.stderr).strip()
        )
        return None
    try:
        prs = json.loads(_text(result.stdout))
    except ValueError:
        return None
    if not isinstance(prs, list):
        return None
    for pr in prs:
        if not (isinstance(pr, dict) and isinstance(pr.get("number"), int) and isinstance(pr.get("baseRefName"), str)):
            return None
    return prs


def _find_stack_root(repo: str, base_branch: str, default_branch: str, env: dict) -> tuple[bool, int | None]:
    """Walk up the open-PR stack from ``base_branch``. Returns (ok, root PR number or None when there is no stack).

    Each step finds the open PR whose head is the current branch and moves to that PR's base. It stops at the
    default branch or when no open PR has the branch as head. ok is False (fail closed) on a read failure,
    malformed output, several PRs for one head name (OD-A), or more than ``_MAX_STACK_DEPTH`` steps.
    """
    branch, root = base_branch, None
    for _ in range(_MAX_STACK_DEPTH):
        if not branch or branch == default_branch:
            return True, root
        prs = _open_prs_with_head(repo, branch, env)
        if prs is None:
            return False, None
        if not prs:
            return True, root
        if len(prs) > 1:
            logger.error("head branch %s has %d open PRs; stack root is ambiguous", branch, len(prs))
            return False, None
        root, branch = prs[0]["number"], prs[0]["baseRefName"]
    if branch and branch != default_branch:
        logger.error("stack deeper than %d PRs from base %s; giving up", _MAX_STACK_DEPTH, base_branch)
        return False, None
    return True, root


def _read_root_required_names(repo: str, root: int, env: dict) -> set[str] | None:
    """Required check names of the root PR. Empty set = no required checks. None = read failure (incl. pending)."""
    args = ["pr", "checks", str(root), "--repo", repo, "--required", "--json", "name"]
    try:
        result = _gh(args, env)
    except subprocess.TimeoutExpired:
        logger.exception("gh pr checks timed out for stack root #%d", root)
        return None
    stderr = _text(result.stderr)
    if result.returncode == 1 and _NO_REQUIRED_CHECKS_TEXT in stderr:
        return set()
    if result.returncode != 0:  # includes exit 8 (pending required checks at the root): fail closed
        logger.error("stack root #%d: gh pr checks failed (exit %d): %s", root, result.returncode, stderr.strip())
        return None
    try:
        entries = json.loads(_text(result.stdout))
    except ValueError:
        return None
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("name"), str) for e in entries
    ):
        return None
    return {e["name"] for e in entries}


def _fallback_checks(
    repo: str, pr_number: int, base_branch: str, default_branch: str, env: dict
) -> tuple[list[Check], list[str]] | None:
    """Checks to judge when the PR's own base has no required checks: (checks, missing required names), None = failure.

    With a stack root that has required checks, only those names count (a name with no run on the PR is
    missing). Otherwise all checks of the PR count, as before.
    """
    ok, root = _find_stack_root(repo, base_branch, default_branch, env)
    if not ok:
        return None
    required = _read_root_required_names(repo, root, env) if root is not None else set()
    if required is None:
        return None
    checks = _read_buckets(repo, pr_number, env, required=False)
    if checks is None:
        return None
    if not required:
        return checks, []
    logger.info("PR #%d: required checks from stack root #%s: %s", pr_number, root, sorted(required))
    return [c for c in checks if c.name in required], sorted(required - {c.name for c in checks})


def _repo_has_workflows(repo: str, env: dict) -> bool | None:
    """True when the repo has an active workflow; None on read failure."""
    result = _gh(["api", f"repos/{repo}/actions/workflows?per_page=100"], env)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(_text(result.stdout))
    except ValueError:
        return None
    workflows = data.get("workflows") if isinstance(data, dict) else None
    if not isinstance(workflows, list):
        return None
    return any(isinstance(w, dict) and w.get("state") == "active" for w in workflows)


def _check_ci(repo: str, pr_number: int, base_branch: str, env: dict) -> SafetyResult | None:
    """None when CI allows approval; otherwise the SKIP or READ_FAILED result (head SHA not set).

    ``base_branch`` is the PR's base (``baseRefName``). The repository default branch is looked up eagerly
    (an extra ``gh repo view`` call); a failed lookup is READ_FAILED. When the PR's own base has required
    checks they are judged and nothing else is read. Otherwise ``_fallback_checks`` applies the stack-root rule.
    ``ci_fix._read_failing_checks`` deliberately does not follow the stack-root rule (see docs/commands/ci-fix.md).
    """
    default_branch = _read_default_branch(repo, env)
    if default_branch is None:
        return SafetyResult(SafetyStatus.READ_FAILED, "default branch read failed")
    logger.debug("PR #%d: checking CI (base %s, default branch %s)", pr_number, base_branch, default_branch)
    checks = _read_buckets(repo, pr_number, env, required=True)
    missing: list[str] = []
    if checks is not None and not checks:
        logger.info("PR #%d: no required checks on base, looking for a stack root", pr_number)
        fallback = _fallback_checks(repo, pr_number, base_branch, default_branch, env)
        checks, missing = fallback if fallback is not None else (None, [])
    if checks is None:
        return SafetyResult(SafetyStatus.READ_FAILED, "checks read failed")
    if missing:
        logger.info("PR #%d: required checks with no run: %s", pr_number, ", ".join(missing))
        return SafetyResult(SafetyStatus.SKIP, REASON_CI_BLOCKED)
    if not checks:
        # Empty is also what gh reports before a push's workflows register, so only trust it when no CI exists.
        has_workflows = _repo_has_workflows(repo, env)
        if has_workflows is None:
            return SafetyResult(SafetyStatus.READ_FAILED, "workflows read failed")
        if has_workflows:
            logger.info("PR #%d: no checks yet but repo has workflows, treating CI as pending", pr_number)
            return SafetyResult(SafetyStatus.SKIP, REASON_CI_BLOCKED)
        logger.info("PR #%d: no checks and no workflows, nothing blocks approval", pr_number)
        return None
    blocking = sorted({c.bucket for c in checks if c.bucket not in _PASSING_BUCKETS})
    logger.info("PR #%d: %d checks, blocking buckets: %s", pr_number, len(checks), blocking or "none")
    if blocking:
        _log_failing_checks(pr_number, checks)
        return SafetyResult(SafetyStatus.SKIP, REASON_CI_BLOCKED)
    return None


def _log_failing_checks(pr_number: int, checks: list[Check]) -> None:
    """Name the failing checks (``fail`` bucket), capped at ``_MAX_NAMED_CHECKS``, and log how many were omitted.

    Pending, cancelled and unrecognised buckets still block but are not named: they appear only in the
    "blocking buckets" line.
    """
    failing = sorted(f"{c.name}({c.bucket})" for c in checks if c.bucket == "fail")
    if not failing:
        return
    named = ", ".join(failing[:_MAX_NAMED_CHECKS])
    omitted = len(failing) - _MAX_NAMED_CHECKS
    suffix = f" (+{omitted} more omitted)" if omitted > 0 else ""
    logger.info("PR #%d: failing checks: %s%s", pr_number, named, suffix)


def _read_pr_state(repo: str, pr_number: int, env: dict) -> dict | None:
    result = _gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "mergeable,autoMergeRequest,isDraft,state,headRefOid,baseRefName",
        ],
        env,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(_text(result.stdout))
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("headRefOid") or "state" not in data:
        return None
    return data


def _state_skip_reason(pr: dict) -> str | None:
    if pr.get("state") != "OPEN":
        return REASON_NOT_OPEN
    if pr.get("isDraft"):
        return REASON_DRAFT
    if pr.get("mergeable") == "CONFLICTING":
        return REASON_CONFLICTING
    if pr.get("mergeable") not in ("MERGEABLE", "CONFLICTING"):
        return REASON_MERGEABLE_UNKNOWN
    if pr.get("autoMergeRequest"):
        return REASON_AUTO_MERGE
    return None


def _already_approved(reviews: list[dict], acting: str) -> bool:
    """Any APPROVED review by the acting account, on any commit (FR-6.4, D-14)."""
    return any(r.get("state") == "APPROVED" and _login(r) == acting for r in reviews if isinstance(r, dict))


def check_approval_safety(repo: str, pr_number: int, current_user: str, env: dict) -> SafetyResult:
    """Read-only approval safety check (FR-8). Never raises for read problems: returns READ_FAILED."""
    try:
        result = _check_approval_safety(repo, pr_number, current_user, env)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.exception("PR #%d: approval safety read raised", pr_number)
        result = SafetyResult(SafetyStatus.READ_FAILED, "read raised an exception")
    logger.info(
        "PR #%d: approval safety %s (%s) head=%s", pr_number, result.status.value, result.reason, result.head_sha
    )
    return result


def _check_approval_safety(repo: str, pr_number: int, current_user: str, env: dict) -> SafetyResult:
    acting = current_user.lower()
    if not acting:
        return SafetyResult(SafetyStatus.READ_FAILED, "acting account unknown")
    pr = _read_pr_state(repo, pr_number, env)
    if pr is None:
        return SafetyResult(SafetyStatus.READ_FAILED, "PR state read failed")
    head = pr["headRefOid"]
    logger.info(
        "PR #%d: state=%s draft=%s mergeable=%s autoMerge=%s",
        pr_number,
        pr.get("state"),
        pr.get("isDraft"),
        pr.get("mergeable"),
        bool(pr.get("autoMergeRequest")),
    )
    reason = _state_skip_reason(pr)
    if reason:
        return SafetyResult(SafetyStatus.SKIP, reason, head)

    reviews = fetch_all_pages(f"repos/{repo}/pulls/{pr_number}/reviews", env)
    if reviews is None:
        return SafetyResult(SafetyStatus.READ_FAILED, "reviews read failed", head)
    if _already_approved(reviews, acting):
        return SafetyResult(SafetyStatus.SKIP, REASON_ALREADY_APPROVED, head)

    blocked = _check_ci(repo, pr_number, pr.get("baseRefName", ""), env)
    if blocked is not None:
        return SafetyResult(blocked.status, blocked.reason, head)
    return SafetyResult(SafetyStatus.SAFE, "all safety checks passed", head)


# ---------------------------------------------------------------------------
# Writes (FR-4 viewed marking, FR-6 approval). Every write honours dry_run.
# ---------------------------------------------------------------------------

_FILES_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      files(first: 100, after: $cursor) {
        nodes { path viewerViewedState additions deletions changeType }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

_MARK_VIEWED_MUTATION = """
mutation($pullRequestId: ID!, $path: String!) {
  markFileAsViewed(input: {pullRequestId: $pullRequestId, path: $path}) {
    clientMutationId
  }
}
"""

VIEWED_STATE = "VIEWED"
_MAX_FILE_PAGES = 100


class ViewedStatus(Enum):
    DONE = "done"  # reads succeeded; see marked / failed
    GATE_NOT_PASSED = "gate_not_passed"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class ViewedResult:
    status: ViewedStatus
    marked: list[str] = field(default_factory=list)  # marked, or would be marked in dry-run
    skipped: list[str] = field(default_factory=list)  # already VIEWED
    failed: list[str] = field(default_factory=list)  # a markFileAsViewed call failed
    all_viewed: bool = False  # every file of the PR is VIEWED (or was just marked, or would be in dry-run)


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


def _split_repo(repo: str) -> tuple[str, str] | None:
    owner, _, name = repo.partition("/")
    return (owner, name) if owner and name else None


def _read_files_page(owner: str, name: str, pr_number: int, cursor: str | None, env: dict) -> dict | None:
    args = ["api", "graphql", "-f", f"query={_FILES_QUERY}", "-F", f"owner={owner}", "-F", f"repo={name}"]
    args += ["-F", f"number={pr_number}"]
    if cursor:
        args += ["-f", f"cursor={cursor}"]
    result = _gh(args, env)
    if result.returncode != 0:
        logger.error("PR #%d: files GraphQL read failed (exit %d)", pr_number, result.returncode)
        return None
    try:
        data = json.loads(_text(result.stdout))
        pr = data["data"]["repository"]["pullRequest"]
        files = pr["files"]
        if not pr.get("id") or not isinstance(files["nodes"], list) or "hasNextPage" not in files["pageInfo"]:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return pr


def read_viewed_states(repo: str, pr_number: int, env: dict) -> tuple[str, dict[str, str]] | None:
    """Return (PR node ID, {path: viewerViewedState}) across all pages, or None on any read failure."""
    parts = _split_repo(repo)
    if parts is None:
        return None
    node_id = ""
    states: dict[str, str] = {}
    cursor: str | None = None
    for _ in range(_MAX_FILE_PAGES):
        pr = _read_files_page(parts[0], parts[1], pr_number, cursor, env)
        if pr is None:
            return None
        node_id = pr["id"]
        for node in pr["files"]["nodes"]:
            if not isinstance(node, dict) or not isinstance(node.get("path"), str):
                return None
            states[node["path"]] = node.get("viewerViewedState") or ""
        info = pr["files"]["pageInfo"]
        if not info["hasNextPage"]:
            return node_id, states
        cursor = info.get("endCursor")
        if not cursor:
            return None
    return None  # page cap reached: incomplete read


def _mark_one(node_id: str, path: str, pr_number: int, env: dict) -> bool:
    result = _gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={_MARK_VIEWED_MUTATION}",
            "-f",
            f"pullRequestId={node_id}",
            "-f",
            f"path={path}",
        ],
        env,
    )
    if result.returncode != 0:
        logger.error("PR #%d: markFileAsViewed failed for %s (exit %d)", pr_number, path, result.returncode)
        return False
    try:
        return not json.loads(_text(result.stdout)).get("errors")
    except (ValueError, AttributeError):
        return False


def _mark_candidates(file_counts: dict[str, int], viewed_max_lines: int, states: dict[str, str]) -> tuple[list, list]:
    """Split counted files strictly below the threshold into (to mark, already viewed).

    Files the PR file list does not contain are left alone."""
    to_mark: list[str] = []
    already: list[str] = []
    for path, lines in file_counts.items():
        if lines >= viewed_max_lines or path not in states:
            continue
        (already if states[path] == VIEWED_STATE else to_mark).append(path)
    return to_mark, already


def mark_files_viewed(
    repo: str,
    pr_number: int,
    file_counts: dict[str, int],
    viewed_max_lines: int,
    gate_passed: bool,
    env: dict,
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
        read = read_viewed_states(repo, pr_number, env)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.exception("PR #%d: viewed state read raised", pr_number)
        read = None
    if read is None:
        return ViewedResult(ViewedStatus.READ_FAILED)
    node_id, states = read
    to_mark, already = _mark_candidates(file_counts, viewed_max_lines, states)
    marked: list[str] = []
    failed: list[str] = []
    for path in to_mark:
        if dry_run:
            logger.info("PR #%d: [dry-run] would mark %s viewed", pr_number, path)
            marked.append(path)
        elif _mark_one(node_id, path, pr_number, env):
            logger.info("PR #%d: marked %s viewed", pr_number, path)
            marked.append(path)
        else:
            failed.append(path)
    for path in already:
        logger.info("PR #%d: %s already viewed, skipping", pr_number, path)
    viewed_now = {*marked, *(p for p, state in states.items() if state == VIEWED_STATE)}
    all_viewed = bool(states) and all(path in viewed_now for path in states)
    return ViewedResult(ViewedStatus.DONE, marked=marked, skipped=already, failed=failed, all_viewed=all_viewed)


def _read_approval_state(repo: str, pr_number: int, acting: str, env: dict) -> bool | None:
    """Tri-state: True = acting account already approved (any commit), False = not, None = read failed."""
    reviews = fetch_all_pages(f"repos/{repo}/pulls/{pr_number}/reviews", env)
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


def _post_approval(repo: str, pr_number: int, head_sha: str, env: dict) -> bool:
    result = _gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "-f",
            "event=APPROVE",
            "-f",
            f"commit_id={head_sha}",
            "-f",
            "body=",
        ],
        env,
    )
    if result.returncode != 0:
        logger.error(
            "PR #%d: approval POST failed (exit %d): %s", pr_number, result.returncode, _text(result.stderr).strip()
        )
        return False
    return True


def _approve(repo: str, pr_number: int, head_sha: str, current_user: str, env: dict, dry_run: bool) -> ApprovalResult:
    acting = current_user.lower()
    if not acting:
        return ApprovalResult(ApprovalStatus.READ_FAILED, "acting account unknown")
    approved = _read_approval_state(repo, pr_number, acting, env)
    if approved is None:
        return ApprovalResult(ApprovalStatus.READ_FAILED, "reviews read failed")
    if approved:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_ALREADY_APPROVED)
    pr = _read_pr_state(repo, pr_number, env)
    if pr is None:
        return ApprovalResult(ApprovalStatus.READ_FAILED, "PR state read failed")
    if pr["headRefOid"] != head_sha:
        return ApprovalResult(ApprovalStatus.SKIPPED, REASON_HEAD_MOVED)
    if dry_run:
        logger.info("PR #%d: [dry-run] would approve at %s", pr_number, head_sha)
        return ApprovalResult(ApprovalStatus.DRY_RUN, REASON_DRY_RUN)
    if not _post_approval(repo, pr_number, head_sha, env):
        return ApprovalResult(ApprovalStatus.WRITE_FAILED, "approval POST failed")
    logger.info("PR #%d: approved at %s", pr_number, head_sha)
    return ApprovalResult(ApprovalStatus.APPROVED, "approved")


def approve_pr(
    repo: str,
    pr_number: int,
    gate: GateResult,
    safety: SafetyResult,
    size: SizeEvaluation,
    approve_max_lines: int,
    current_user: str,
    env: dict,
    dry_run: bool = False,
    all_viewed: bool = False,
) -> ApprovalResult:
    """Approve a PR strictly below ``approve_max_lines``, or any size once every file is viewed (FR-6).

    ``all_viewed`` (every file of the PR marked viewed, by the reviewer or by this tool) waives only
    the size limit; the gate, safety, idempotency and head-binding rules still apply. Never raises
    for read problems.

    Owns the whole decision: requires ``gate`` PASSED and ``safety`` SAFE, binding the approval to
    ``safety.head_sha``. Re-reads reviews (tri-state) and the current head; any failed read or
    moved head means no write. The review body is empty and ``commit_id`` is the head SHA.
    """
    result = _approval_precheck(gate, safety, size.total_lines, approve_max_lines, all_viewed)
    if result is None:
        try:
            result = _approve(repo, pr_number, safety.head_sha, current_user, env, dry_run)
        except (subprocess.SubprocessError, OSError, ValueError):
            logger.exception("PR #%d: approval raised", pr_number)
            result = ApprovalResult(ApprovalStatus.READ_FAILED, "approval raised an exception")
    return result
