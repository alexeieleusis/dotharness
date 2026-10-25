"""Reading a PR's CI checks: the latest run of each check and the stack-root rule.

Shared by ``auto_approve_github`` (approval safety), ``merge`` and ``ci_fix``. ``read_checks`` is the one entry
point; the callers decide what the checks mean.

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
from datetime import UTC, datetime
from typing import NamedTuple

from harness.runners.common import TIMEOUT_GH, run_cmd

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


def gh(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return run_cmd(["gh", *args], cwd="/", env=env, timeout=TIMEOUT_GH, check=False)


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


def _read_buckets(repo: str, pr_number: int, env: dict, required: bool) -> list[Check] | None:
    """Latest run of each check (name, bucket) for the PR. None = read failure; [] with required=True = no required checks."""
    args = ["pr", "checks", str(pr_number), "--repo", repo, "--json", _CHECK_FIELDS]
    result = gh([*args, "--required"] if required else args, env)
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


def _read_default_branch(repo: str, env: dict) -> str | None:
    """The repository default branch (``gh repo view --json defaultBranchRef``), or None on any failure.

    Failure means a non-zero exit, a timeout, unparseable JSON, or a missing or empty ``defaultBranchRef.name``.
    """
    try:
        result = gh(["repo", "view", repo, "--json", "defaultBranchRef"], env)
    except subprocess.TimeoutExpired:
        logger.exception("gh repo view timed out for %s", repo)
        return None
    if result.returncode != 0:
        logger.error("gh repo view failed for %s (exit %d): %s", repo, result.returncode, text(result.stderr).strip())
        return None
    try:
        name = json.loads(text(result.stdout))["defaultBranchRef"]["name"]
    except (ValueError, KeyError, TypeError):
        return None
    return name if isinstance(name, str) and name else None


def _open_prs_with_head(repo: str, branch: str, env: dict) -> list[dict] | None:
    """Open PRs whose head branch is ``branch`` (name only, D-5), as ``{number, baseRefName}``; None on failure."""
    args = ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,baseRefName"]
    try:
        result = gh(args, env)
    except subprocess.TimeoutExpired:
        logger.exception("gh pr list timed out for head %s", branch)
        return None
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
        result = gh(args, env)
    except subprocess.TimeoutExpired:
        logger.exception("gh pr checks timed out for stack root #%d", root)
        return None
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


def _fallback_checks(repo: str, pr_number: int, base_branch: str, default_branch: str, env: dict) -> CheckRead | None:
    """Checks to judge when the PR's own base has no required checks; None = failure.

    With a stack root that has required checks, only those names count (a name with no run on the PR is
    missing). Otherwise all checks of the PR count.
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
        return CheckRead(checks, [])
    logger.info("PR #%d: required checks from stack root #%s: %s", pr_number, root, sorted(required))
    return CheckRead([c for c in checks if c.name in required], sorted(required - {c.name for c in checks}))


def read_checks(repo: str, pr_number: int, base_branch: str, env: dict, *, stack_root: bool = True) -> CheckRead | None:
    """The latest run of each check judged for the PR, or None on any read failure (fail closed).

    The PR's required checks when it has any. Otherwise, with ``stack_root`` the stack-root rule applies (the
    repository default branch is looked up first, an extra ``gh repo view`` call), and without it all the PR's
    checks are returned.
    """
    default_branch = None
    if stack_root:
        default_branch = _read_default_branch(repo, env)
        if default_branch is None:
            return None
        logger.debug("PR #%d: reading checks (base %s, default branch %s)", pr_number, base_branch, default_branch)
    checks = _read_buckets(repo, pr_number, env, required=True)
    if checks is None:
        return None
    if checks:
        return CheckRead(checks, [])
    if default_branch is None:
        all_checks = _read_buckets(repo, pr_number, env, required=False)
        return None if all_checks is None else CheckRead(all_checks, [])
    logger.info("PR #%d: no required checks on base, looking for a stack root", pr_number)
    return _fallback_checks(repo, pr_number, base_branch, default_branch, env)


def repo_has_workflows(repo: str, env: dict) -> bool | None:
    """True when the repo has an active workflow; None on read failure."""
    result = gh(["api", f"repos/{repo}/actions/workflows?per_page=100"], env)
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
