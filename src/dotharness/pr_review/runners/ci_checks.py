"""Reading a PR's CI checks: the latest run of each check and the stack-root rule.

Shared by ``auto_approve_github`` (approval safety), ``merge`` and ``ci_fix``. ``read_checks`` (legacy ``gh``
transport, used by ``auto_approve_github`` until it is migrated) and ``read_host_checks`` (``CodeHost``, used by
``merge`` and ``ci_fix``) are the entry points; the callers decide what the checks mean. The host section at the
end of the module has the same rules, with host check statuses in place of buckets.

Runs are keyed by (``workflow``, ``name``); among several runs of one key the latest by ``completedAt`` (else
``startedAt``) decides, so a failed run followed by a passing rerun does not block (see ``latest_runs``).

Stack-root rule (Phase 3b, ``stack_root=True``): when the PR's own base has no required checks (a stacked PR's
base is another PR's unprotected head branch), the base chain is walked through open PRs matched by head branch
name only (``_find_stack_root``) up to the default branch. Only the check names required at the root PR are then
evaluated on this PR; a required name with no run on it is reported in ``CheckRead.missing``, and an empty required
set with no checks is an empty read. With no stack, or a root with no required checks, all checks are returned.
OD-A: more than one open PR for one head name is ambiguous and fails closed (None). A walk deeper than
``_MAX_STACK_DEPTH``, any read failure, malformed output, or pending required checks at the root also fail
closed. A PR whose own base has required checks makes no walk calls. With ``stack_root=False`` no default-branch
lookup or walk is made and a PR with no required checks on its base returns all its checks.
"""

import json
import logging
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import NamedTuple, TypeVar

from dotharness.code_host import (
    ChangeRequest,
    ChangeRequestState,
    CheckStatus,
    CodeHost,
    CodeHostError,
    MergeableState,
    RepositoryRef,
    UnsupportedCapabilityError,
)
from dotharness.code_host import Check as HostCheck
from dotharness.gh import gh_argv
from dotharness.pr_review.runners.common import TIMEOUT_GH, call_with_policy, run_cmd

logger = logging.getLogger(__name__)

PASSING_BUCKETS = frozenset({"pass", "skipping"})
UNNAMED_CHECK = "<unnamed>"
_NO_REQUIRED_CHECKS_TEXT = "no required checks reported"
_NO_CHECKS_TEXT = "no checks reported"
_MAX_STACK_DEPTH = 10
_CHECK_FIELDS = "name,bucket,link,workflow,startedAt,completedAt"


class Check(NamedTuple):
    name: str
    bucket: str
    link: str | None = None
    workflow: str = ""  # workflow name as reported by gh; "" when unknown


class CheckRead(NamedTuple):
    checks: list[Check]
    missing: list[str]  # required names (stack root) with no run on the PR


_TIMEOUT_EXIT = 124  # synthetic exit code for a timed-out gh call (as coreutils ``timeout``)


async def gh(args: list[str], env: dict) -> subprocess.CompletedProcess:
    """Run gh; a timeout becomes a synthetic failed result so every caller fails closed the same way."""
    try:
        return await run_cmd(gh_argv(*args), cwd="/", env=env, timeout=TIMEOUT_GH, check=False)
    except subprocess.TimeoutExpired:
        logger.exception("gh %s timed out after %ss", " ".join(args[:2]), TIMEOUT_GH)
        return subprocess.CompletedProcess(gh_argv(*args), _TIMEOUT_EXIT, stdout="", stderr="timed out")


def text(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_ZERO_YEAR = 1  # gh reports an unset completedAt as 0001-01-01T00:00:00Z


def _check_ts(value: object) -> datetime | None:
    """A usable check timestamp (aware, not the zero value), else None."""
    ts = parse_ts(value)
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
        link, workflow = check.get("link"), check.get("workflow")
        parsed.append(
            Check(
                name if isinstance(name, str) and name else UNNAMED_CHECK,
                check["bucket"],
                link if isinstance(link, str) else None,
                workflow if isinstance(workflow, str) else "",
            )
        )
    return parsed


async def _read_buckets(repo: str, pr_number: int, env: dict, required: bool) -> list[Check] | None:
    """Latest run of each check (name, bucket) for the PR. None = read failure; [] with required=True = no required checks."""
    args = ["pr", "checks", str(pr_number), "--repo", repo, "--json", _CHECK_FIELDS]
    result = await gh([*args, "--required"] if required else args, env)
    stderr = text(result.stderr)
    if required and result.returncode == 1 and _NO_REQUIRED_CHECKS_TEXT in stderr:
        return []
    if not required and result.returncode == 1 and _NO_CHECKS_TEXT in stderr:
        return []
    if result.returncode == 8 and not text(result.stdout).strip():
        return None  # pending exit without JSON: cannot decide from buckets
    if result.returncode not in (0, 8):
        logger.error("PR #%d: gh pr checks failed (exit %d): %s", pr_number, result.returncode, stderr.strip())
        return None
    return _parse_buckets(text(result.stdout))


async def _read_default_branch(repo: str, env: dict) -> str | None:
    """The repository default branch (``gh repo view --json defaultBranchRef``), or None on any failure.

    Failure means a non-zero exit, a timeout, unparseable JSON, or a missing or empty ``defaultBranchRef.name``.
    """
    result = await gh(["repo", "view", repo, "--json", "defaultBranchRef"], env)
    if result.returncode != 0:
        logger.error("gh repo view failed for %s (exit %d): %s", repo, result.returncode, text(result.stderr).strip())
        return None
    try:
        name = json.loads(text(result.stdout))["defaultBranchRef"]["name"]
    except (ValueError, KeyError, TypeError):
        return None
    return name if isinstance(name, str) and name else None


async def _open_prs_with_head(repo: str, branch: str, env: dict) -> list[dict] | None:
    """Open PRs whose head branch is ``branch`` (name only, D-5), as ``{number, baseRefName}``; None on failure."""
    args = ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,baseRefName"]
    result = await gh(args, env)
    if result.returncode != 0:
        logger.error(
            "gh pr list --head %s failed (exit %d): %s", branch, result.returncode, text(result.stderr).strip()
        )
        return None
    try:
        prs = json.loads(text(result.stdout))
    except ValueError:
        return None
    if not isinstance(prs, list):
        return None
    for pr in prs:
        if not (isinstance(pr, dict) and isinstance(pr.get("number"), int) and isinstance(pr.get("baseRefName"), str)):
            return None
    return prs


async def _find_stack_root(repo: str, base_branch: str, default_branch: str, env: dict) -> tuple[bool, int | None]:
    """Walk up the open-PR stack from ``base_branch``. Returns (ok, root PR number or None when there is no stack).

    Each step finds the open PR whose head is the current branch and moves to that PR's base. It stops at the
    default branch or when no open PR has the branch as head. ok is False (fail closed) on a read failure,
    malformed output, several PRs for one head name (OD-A), or more than ``_MAX_STACK_DEPTH`` steps.
    """
    branch, root = base_branch, None
    for _ in range(_MAX_STACK_DEPTH):
        if not branch or branch == default_branch:
            return True, root
        prs = await _open_prs_with_head(repo, branch, env)
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


async def _read_root_required_names(repo: str, root: int, env: dict) -> set[str] | None:
    """Required check names of the root PR. Empty set = no required checks. None = read failure (incl. pending)."""
    args = ["pr", "checks", str(root), "--repo", repo, "--required", "--json", "name"]
    result = await gh(args, env)
    stderr = text(result.stderr)
    if result.returncode == 1 and _NO_REQUIRED_CHECKS_TEXT in stderr:
        return set()
    if result.returncode != 0:  # includes exit 8 (pending required checks at the root): fail closed
        logger.error("stack root #%d: gh pr checks failed (exit %d): %s", root, result.returncode, stderr.strip())
        return None
    try:
        entries = json.loads(text(result.stdout))
    except ValueError:
        return None
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("name"), str) for e in entries
    ):
        return None
    return {e["name"] for e in entries}


async def _fallback_checks(
    repo: str, pr_number: int, base_branch: str, default_branch: str, env: dict
) -> CheckRead | None:
    """Checks to judge when the PR's own base has no required checks; None = failure.

    With a stack root that has required checks, only those names count (a name with no run on the PR is
    missing). Otherwise all checks of the PR count.
    """
    ok, root = await _find_stack_root(repo, base_branch, default_branch, env)
    if not ok:
        return None
    required = await _read_root_required_names(repo, root, env) if root is not None else set()
    if required is None:
        return None
    checks = await _read_buckets(repo, pr_number, env, required=False)
    if checks is None:
        return None
    if not required:
        return CheckRead(checks, [])
    logger.info("PR #%d: required checks from stack root #%s: %s", pr_number, root, sorted(required))
    return CheckRead([c for c in checks if c.name in required], sorted(required - {c.name for c in checks}))


async def read_checks(
    repo: str, pr_number: int, base_branch: str, env: dict, *, stack_root: bool = True
) -> CheckRead | None:
    """The latest run of each check judged for the PR, or None on any read failure (fail closed).

    The PR's required checks when it has any. Otherwise, with ``stack_root`` the stack-root rule applies (the
    repository default branch is looked up first, an extra ``gh repo view`` call), and without it all the PR's
    checks are returned.
    """
    default_branch = None
    if stack_root:
        default_branch = await _read_default_branch(repo, env)
        if default_branch is None:
            return None
        logger.debug("PR #%d: reading checks (base %s, default branch %s)", pr_number, base_branch, default_branch)
    checks = await _read_buckets(repo, pr_number, env, required=True)
    if checks is None:
        return None
    if checks:
        return CheckRead(checks, [])
    if default_branch is None:
        all_checks = await _read_buckets(repo, pr_number, env, required=False)
        return None if all_checks is None else CheckRead(all_checks, [])
    logger.info("PR #%d: no required checks on base, looking for a stack root", pr_number)
    return await _fallback_checks(repo, pr_number, base_branch, default_branch, env)


async def repo_has_workflows(repo: str, env: dict) -> bool | None:
    """True when the repo has an active workflow; None on read failure."""
    result = await gh(["api", f"repos/{repo}/actions/workflows?per_page=100"], env)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(text(result.stdout))
    except ValueError:
        return None
    workflows = data.get("workflows") if isinstance(data, dict) else None
    if not isinstance(workflows, list):
        return None
    return any(isinstance(w, dict) and w.get("state") == "active" for w in workflows)


# ---------------------------------------------------------------------------
# Host-based readers (``CodeHost``). Used by ``merge`` and ``ci_fix``.
#
# Everything above this banner is the legacy ``gh`` transport. It stays only because ``auto_approve_github`` (Phase
# 12 of the code-host migration) still imports ``gh``, ``Check``, ``read_checks`` and ``repo_has_workflows``; that
# phase deletes it. New callers use the functions below.
# ---------------------------------------------------------------------------

PASSING_STATUSES = frozenset({CheckStatus.PASS, CheckStatus.SKIPPING})
CI_BLOCKED = "ci-blocked"
REASON_NOT_OPEN = "not-open"
REASON_DRAFT = "draft"
REASON_CONFLICTING = "conflicting"
REASON_MERGEABLE_UNKNOWN = "mergeable-unknown"
REASON_AUTO_MERGE = "auto-merge"
_MAX_NAMED_CHECKS = 10
_REQUIRED_SITE = "required_checks"

_T = TypeVar("_T")


class HostCheckRead(NamedTuple):
    checks: list[HostCheck]
    missing: list[str]  # required names (stack root) with no run on the change request


class CiBlock(NamedTuple):
    """Why CI does not allow acting. ``read_failed`` means the answer is unknown, not that CI is red."""

    read_failed: bool
    reason: str


async def guarded_read(site: str, operation: str, call: Callable[[], Awaitable[_T]], what: str) -> _T | None:
    """Await a host read; None (logged) on any ``CodeHostError``. Fails closed for the callers.

    ``UnsupportedCapabilityError`` is handled by the policy of ``(site, operation)``: stop re-raises it past the
    broad handler below, degrade and warn give None.
    """
    try:
        return await call_with_policy(site, operation, call, None)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError:
        logger.exception("%s failed", what)
        return None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _host_check_key(check: HostCheck) -> tuple[str, str]:
    return (check.workflow_name or "", check.name)


def _newest_host_checks(runs: list[HostCheck]) -> list[HostCheck] | None:
    if len(runs) == 1:
        return runs
    stamped = []
    for run in runs:
        completed = _utc(run.completed_at)
        when = completed or _utc(run.started_at)
        if when is None:
            return None
        stamped.append((when, completed is not None, run))
    newest = max(when for when, _, _ in stamped)
    return [run if done else replace(run, status=CheckStatus.PENDING) for when, done, run in stamped if when == newest]


def latest_checks(checks: list[HostCheck] | tuple[HostCheck, ...]) -> list[HostCheck] | None:
    """Keep only the latest run of each check (key = workflow name + name); None = cannot decide (fail closed).

    Same rule as ``latest_runs``: a key with one run needs no timestamps; with several runs the greatest
    ``completed_at`` wins, falling back to ``started_at``; a run with neither makes the result None; a latest run
    with no ``completed_at`` counts as pending; ties are all kept; first-seen key order.
    """
    groups: dict[tuple[str, str], list[HostCheck]] = {}
    for check in checks:
        groups.setdefault(_host_check_key(check), []).append(check)
    kept: list[HostCheck] = []
    for runs in groups.values():
        newest = _newest_host_checks(runs)
        if newest is None:
            return None
        kept.extend(newest)
    return [c if c.name else replace(c, name=UNNAMED_CHECK) for c in kept]


async def _list_checks(host: CodeHost, repo: RepositoryRef, number: int, *, required: bool) -> list[HostCheck] | None:
    found = await guarded_read(
        _REQUIRED_SITE,
        "Checks.list_checks",
        lambda: host.checks.list_checks(repo, number, required_only=required),
        f"PR #{number}: listing {'required ' if required else ''}checks",
    )
    return None if found is None else list(found)


async def _latest_checks_of(
    host: CodeHost, repo: RepositoryRef, number: int, *, required: bool
) -> list[HostCheck] | None:
    found = await _list_checks(host, repo, number, required=required)
    return None if found is None else latest_checks(found)


async def _open_prs_with_head_host(host: CodeHost, repo: RepositoryRef, branch: str) -> list[ChangeRequest] | None:
    """Open change requests whose head branch is ``branch`` (name only, D-5); None on failure."""
    found = await guarded_read(
        _REQUIRED_SITE,
        "ChangeRequests.find_by_branch",
        lambda: host.change_requests.find_by_branch(repo, branch, state=ChangeRequestState.OPEN),
        f"listing open PRs with head {branch}",
    )
    return None if found is None else list(found)


async def _stack_root_host(
    host: CodeHost, repo: RepositoryRef, base_branch: str, default_branch: str
) -> tuple[bool, int | None]:
    """Walk up the open-PR stack from ``base_branch``. Returns (ok, root PR number or None when there is no stack).

    Each step finds the open PR whose head is the current branch and moves to that PR's base. ok is False (fail
    closed) on a read failure, several PRs for one head name (OD-A), or more than ``_MAX_STACK_DEPTH`` steps.
    """
    branch, root = base_branch, None
    for _ in range(_MAX_STACK_DEPTH):
        if not branch or branch == default_branch:
            return True, root
        found = await _open_prs_with_head_host(host, repo, branch)
        if found is None:
            return False, None
        if not found:
            return True, root
        if len(found) > 1:
            logger.error("head branch %s has %d open PRs; stack root is ambiguous", branch, len(found))
            return False, None
        root, branch = found[0].number, found[0].base_reference
    if branch and branch != default_branch:
        logger.error("stack deeper than %d PRs from base %s; giving up", _MAX_STACK_DEPTH, base_branch)
        return False, None
    return True, root


async def _root_required_names_host(host: CodeHost, repo: RepositoryRef, root: int) -> set[str] | None:
    """Required check names of the root PR. Empty set = no required checks. None = read failure."""
    found = await _list_checks(host, repo, root, required=True)
    return None if found is None else {c.name for c in found}


async def _checks_with_stack_fallback_host(
    host: CodeHost, repo: RepositoryRef, number: int, base_branch: str, default_branch: str
) -> HostCheckRead | None:
    ok, root = await _stack_root_host(host, repo, base_branch, default_branch)
    if not ok:
        return None
    required = await _root_required_names_host(host, repo, root) if root is not None else set()
    if required is None:
        return None
    checks = await _latest_checks_of(host, repo, number, required=False)
    if checks is None:
        return None
    if not required:
        return HostCheckRead(checks, [])
    logger.info("PR #%d: required checks from stack root #%s: %s", number, root, sorted(required))
    return HostCheckRead([c for c in checks if c.name in required], sorted(required - {c.name for c in checks}))


async def read_host_checks(
    host: CodeHost, repo: RepositoryRef, number: int, base_branch: str, *, stack_root: bool = True
) -> HostCheckRead | None:
    """The latest run of each check judged for the change request, or None on any read failure (fail closed).

    Same rules as ``read_checks``: the required checks when there are any, else the stack-root rule (with
    ``stack_root``, after a default-branch lookup) or all checks.
    """
    default_branch = None
    if stack_root:
        default_branch = await guarded_read(
            _REQUIRED_SITE,
            "Repository.default_branch",
            lambda: host.repository.default_branch(repo),
            "reading the default branch",
        )
        if not default_branch:
            return None
        logger.debug("PR #%d: reading checks (base %s, default branch %s)", number, base_branch, default_branch)
    checks = await _latest_checks_of(host, repo, number, required=True)
    if checks is None:
        return None
    if checks:
        return HostCheckRead(checks, [])
    if default_branch is None:
        everything = await _latest_checks_of(host, repo, number, required=False)
        return None if everything is None else HostCheckRead(everything, [])
    logger.info("PR #%d: no required checks on base, looking for a stack root", number)
    return await _checks_with_stack_fallback_host(host, repo, number, base_branch, default_branch)


async def host_has_workflows(host: CodeHost, repo: RepositoryRef) -> bool | None:
    """True when the repository has workflows; None on read failure or an unsupported ``Checks.list_workflows``."""
    found = await guarded_read(
        "ci_checks_workflows",
        "Checks.list_workflows",
        lambda: host.checks.list_workflows(repo),
        "listing workflows",
    )
    return None if found is None else bool(found)


def _log_failing_host_checks(number: int, checks: list[HostCheck]) -> None:
    failing = sorted(f"{c.name}({c.status.value})" for c in checks if c.status is CheckStatus.FAIL)
    if not failing:
        return
    named = ", ".join(failing[:_MAX_NAMED_CHECKS])
    omitted = len(failing) - _MAX_NAMED_CHECKS
    suffix = f" (+{omitted} more omitted)" if omitted > 0 else ""
    logger.info("PR #%d: failing checks: %s%s", number, named, suffix)


async def ci_blocker(host: CodeHost, repo: RepositoryRef, number: int, base_branch: str) -> CiBlock | None:
    """None when CI allows acting; otherwise why not. Required checks with the stack-root rule."""
    read = await read_host_checks(host, repo, number, base_branch)
    if read is None:
        return CiBlock(True, "checks read failed")
    if read.missing:
        logger.info("PR #%d: required checks with no run: %s", number, ", ".join(read.missing))
        return CiBlock(False, CI_BLOCKED)
    if not read.checks:
        # Empty is also what a host reports before a push's workflows register, so only trust it when no CI exists.
        has_workflows = await host_has_workflows(host, repo)
        if has_workflows is None:
            return CiBlock(True, "workflows read failed")
        if has_workflows:
            logger.info("PR #%d: no checks yet but repo has workflows, treating CI as pending", number)
            return CiBlock(False, CI_BLOCKED)
        logger.info("PR #%d: no checks and no workflows, nothing blocks", number)
        return None
    blocking = sorted({c.status.value for c in read.checks if c.status not in PASSING_STATUSES})
    logger.info("PR #%d: %d checks, blocking statuses: %s", number, len(read.checks), blocking or "none")
    if blocking:
        _log_failing_host_checks(number, read.checks)
        return CiBlock(False, CI_BLOCKED)
    return None


def change_request_skip_reason(change_request: ChangeRequest) -> str | None:
    """Why a change request is not eligible to act on (not open, draft, conflicting, unknown, auto-merge), else None."""
    if change_request.state is not ChangeRequestState.OPEN:
        return REASON_NOT_OPEN
    if change_request.is_draft:
        return REASON_DRAFT
    if change_request.mergeable is MergeableState.CONFLICTING:
        return REASON_CONFLICTING
    if change_request.mergeable is not MergeableState.MERGEABLE:
        return REASON_MERGEABLE_UNKNOWN
    if change_request.auto_merge_enabled:
        return REASON_AUTO_MERGE
    return None


async def read_change_request(host: CodeHost, repo: RepositoryRef, number: int, site: str) -> ChangeRequest | None:
    """One change request, fresh, or None (logged) when it cannot be read. ``site`` selects the policy row."""
    return await guarded_read(
        site,
        "ChangeRequests.get",
        lambda: host.change_requests.get(repo, number),
        f"PR #{number}: reading the change request",
    )


async def list_own_open_change_requests(
    host: CodeHost,
    repo: RepositoryRef,
    current_user: str,
    only_pr: int | None,
    failures: list[str],
    site: str,
) -> list[ChangeRequest]:
    """Open change requests authored by the acting user, or just ``only_pr``, sorted by number.

    A ``CodeHostError`` is appended to ``failures`` (its message) and yields ``[]``. ``UnsupportedCapabilityError``
    follows the policy of ``(site, "ChangeRequests.list")`` and by default stops.
    """

    async def list_all() -> list[ChangeRequest]:
        return list(await host.change_requests.list(repo, state=ChangeRequestState.OPEN, author=current_user or None))

    async def get_one() -> list[ChangeRequest]:
        return [await host.change_requests.get(repo, only_pr)] if only_pr is not None else []

    try:
        if only_pr is None:
            found = await call_with_policy(site, "ChangeRequests.list", list_all, [])
        else:
            found = await call_with_policy(site, "ChangeRequests.get", get_one, [])
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as e:
        logger.exception("listing change requests failed")
        failures.append(str(e))
        return []
    return sorted(found, key=lambda c: c.number)
