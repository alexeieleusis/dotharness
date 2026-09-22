from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from spec_prism_flow.build.errors import CommandError, OrchestrationError, run_subprocess

_GH_TIMEOUT_SECONDS = 60

# reviewThreads paginated by `first`/`after` per GitHub's Relay-style cursor
# convention; 100 is the max page size GitHub's GraphQL API allows for this
# connection, so this minimizes round trips while still exercising pagination
# on any PR with more threads than that.
_UNRESOLVED_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes { isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class GhCommandError(CommandError):
    """A `gh` subprocess exited non-zero (or timed out) for a reason unrelated to
    mergeability -- see `PRNotMergeableError` for the `gh pr merge`-specific case."""


class PRNotMergeableError(GhCommandError, OrchestrationError):
    """Raised when `gh pr merge` exits non-zero. Subclasses both `GhCommandError`
    (for `cmd_args`/`returncode`/`stderr`) and `errors.OrchestrationError` (for
    `exit_code`/`next_command`/`phase_number`/`phase_name`/`with_context()`) via
    multiple inheritance, so a Phase 11 orchestrator that catches
    `OrchestrationError` generically still sees this error -- without editing
    `errors.py`, which is out of this phase's scope. Each base's `__init__` is
    called explicitly since `super()` alone can't thread both sets of constructor
    arguments through the diamond. No internal retry: a single non-zero exit raises
    immediately, matching the "no config-driven retry for merges" requirement."""

    exit_code: ClassVar[int] = 15

    def __init__(self, args: list[str], returncode: int, stderr: str, pr_number: int) -> None:
        GhCommandError.__init__(self, args, returncode, stderr)
        OrchestrationError.__init__(self, str(self), next_command=f"gh pr view {pr_number}")


@dataclass(frozen=True)
class PRHandle:
    number: int
    url: str


@dataclass(frozen=True)
class PRStatus:
    """Fields are the raw string values `gh pr view --json` returns (e.g. `state`:
    "OPEN"/"CLOSED"/"MERGED", `mergeable`: "MERGEABLE"/"CONFLICTING"/"UNKNOWN",
    `review_decision`: "APPROVED"/"CHANGES_REQUESTED"/"REVIEW_REQUIRED"/"" when no
    review has been requested) -- left as `str` rather than an enum since gh's own
    JSON schema for these fields isn't versioned/guaranteed, and callers so far
    (Phase 11) only need to compare against known literal values."""

    state: str
    mergeable: str
    review_decision: str


def _run_raw(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Converts a timeout to `GhCommandError` but leaves a non-zero exit for the
    caller to inspect -- `pr_merge` needs the raw result to wrap into
    `PRNotMergeableError` instead."""
    return run_subprocess(["gh", *args], cwd=cwd, timeout=_GH_TIMEOUT_SECONDS, error_cls=GhCommandError, check=False)


def _run(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    result = _run_raw(cwd, *args)
    if result.returncode != 0:
        raise GhCommandError(["gh", *args], result.returncode, result.stderr)
    return result


def _parse_pr_handle(stdout: str) -> PRHandle:
    """`gh pr create`'s stdout is the created PR's URL on success (occasionally
    preceded by blank lines); the PR number is its final path segment."""
    lines = [line.strip() for line in stdout.strip().splitlines() if line.strip()]
    url = lines[-1]
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    return PRHandle(number=number, url=url)


def pr_create(cwd: Path, branch: str, title: str, body: str) -> PRHandle:
    """`title`/`body` are passed as individual argv elements (never interpolated
    into a shell string), so shell metacharacters in either are inert."""
    result = _run(cwd, "pr", "create", "--head", branch, "--title", title, "--body", body)
    return _parse_pr_handle(result.stdout)


def pr_view(repo: str, pr_number: int) -> PRStatus:
    """`repo` is an explicit `"owner/name"` string passed straight to `gh`'s `-R`,
    mirroring `pr_create`'s explicit-`cwd` style rather than relying on `gh`
    inferring the target from the process's working directory."""
    result = _run(None, "pr", "view", str(pr_number), "-R", repo, "--json", "state,mergeable,reviewDecision")
    data = json.loads(result.stdout)
    return PRStatus(
        state=data["state"],
        mergeable=data["mergeable"],
        review_decision=data.get("reviewDecision") or "",
    )


def unresolved_thread_count(repo: str, pr_number: int) -> int:
    """`repo` is an explicit `"owner/name"` string (see `pr_view`), split directly
    into GraphQL's `owner`/`repo` variables -- no `gh repo view` round trip needed
    since the caller already has it on hand.

    Paginates via `reviewThreads`' Relay-style cursor (`pageInfo.hasNextPage`/
    `endCursor`) rather than trusting a single page, since a PR can accumulate more
    threads than one page holds and callers depend on the exact total."""
    owner, name = repo.split("/", 1)
    cursor: str | None = None
    count = 0
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={_UNRESOLVED_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        result = _run(None, *args)
        data = json.loads(result.stdout)
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        count += sum(1 for node in threads["nodes"] if not node["isResolved"])
        page_info = threads["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return count


def pr_merge(repo: str, pr_number: int) -> None:
    """`repo` is an explicit `"owner/name"` string (see `pr_view`). Squash is the
    only strategy -- there's no config-driven choice (Phase 01's `BuildConfig` has
    no merge-strategy field) -- and a non-zero exit raises immediately with no
    internal retry.

    Calls `_run_raw` (not `_run`) so a timeout -- which `_run_raw` reports as a
    plain `GhCommandError`, since it has no way to know that call is a merge -- is
    also converted to `PRNotMergeableError` here: a client-side timeout leaves
    mergeability just as ambiguous as a non-zero exit, so it needs the same
    `next_command` recovery guidance."""
    argv = ("pr", "merge", str(pr_number), "-R", repo, "--squash")
    try:
        result = _run_raw(None, *argv)
    except GhCommandError as exc:
        raise PRNotMergeableError(exc.cmd_args, exc.returncode, exc.stderr, pr_number) from exc
    if result.returncode != 0:
        raise PRNotMergeableError(["gh", *argv], result.returncode, result.stderr, pr_number)
