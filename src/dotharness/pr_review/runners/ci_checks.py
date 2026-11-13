"""Reading a change request's CI checks: the latest run of each check and the stack-root rule.

Shared by ``auto_approve_github`` (approval safety), ``merge`` and ``ci_fix``. ``read_host_checks`` is the entry
point and ``ci_blocker`` the CI verdict; the callers decide what the checks mean. Everything goes through
``CodeHost``; nothing here builds a host command line or reads a raw host dictionary.

Runs are keyed by (workflow name, name); among several runs of one key the latest by ``completed_at`` (else
``started_at``) decides, so a failed run followed by a passing rerun does not block (see ``latest_checks``).

Stack-root rule (Phase 3b, ``stack_root=True``): when the change request's own base has no required checks (a
stacked PR's base is another PR's unprotected head branch), the base chain is walked through open change requests
matched by head branch name only (``_stack_root_host``) up to the default branch. Only the check names required at
the root are then evaluated on this change request; a required name with no run on it is reported in
``HostCheckRead.missing``, and an empty required set with no checks is an empty read. With no stack, or a root with
no required checks, all checks are returned. OD-A: more than one open change request for one head name is ambiguous
and fails closed (None). A walk deeper than ``_MAX_STACK_DEPTH``, any read failure, or an unsupported capability
under a degrade policy also fails closed. A change request whose own base has required checks makes no walk calls.
With ``stack_root=False`` no default-branch lookup or walk is made and a change request with no required checks on
its base returns all its checks.
"""

import logging
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
from dotharness.pr_review.runners.common import Site, call_with_policy

logger = logging.getLogger(__name__)

UNNAMED_CHECK = "<unnamed>"
_MAX_STACK_DEPTH = 10


def text(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


PASSING_STATUSES = frozenset({CheckStatus.PASS, CheckStatus.SKIPPING})
CI_BLOCKED = "ci-blocked"
REASON_NOT_OPEN = "not-open"
REASON_DRAFT = "draft"
REASON_CONFLICTING = "conflicting"
REASON_MERGEABLE_UNKNOWN = "mergeable-unknown"
REASON_AUTO_MERGE = "auto-merge"
_MAX_NAMED_CHECKS = 10
_REQUIRED_SITE = Site.REQUIRED_CHECKS

_T = TypeVar("_T")


class HostCheckRead(NamedTuple):
    checks: list[HostCheck]
    missing: list[str]  # required names (stack root) with no run on the change request


class CiBlock(NamedTuple):
    """Why CI does not allow acting. ``read_failed`` means the answer is unknown, not that CI is red."""

    read_failed: bool
    reason: str


async def guarded_read(site: Site | str, operation: str, call: Callable[[], Awaitable[_T]], what: str) -> _T | None:
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
        Site.CI_CHECKS_WORKFLOWS,
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


async def read_change_request(
    host: CodeHost, repo: RepositoryRef, number: int, site: Site | str
) -> ChangeRequest | None:
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
    site: Site | str,
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
