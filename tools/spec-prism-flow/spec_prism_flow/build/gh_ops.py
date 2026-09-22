from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from spec_prism_flow.build.errors import CommandError

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


class PRNotMergeableError(GhCommandError):
    """Raised when `gh pr merge` exits non-zero. Subclasses `GhCommandError` (not
    `errors.OrchestrationError`, which is where `next_command` normally lives)
    because `errors.py` is out of this phase's scope to edit -- `next_command` is
    instead set directly as an attribute here, kept as a plain string rather than a
    property so callers can read it the same way they would off an
    OrchestrationError. No internal retry: a single non-zero exit raises
    immediately, matching the "no config-driven retry for merges" requirement."""

    def __init__(self, args: list[str], returncode: int, stderr: str, pr_number: int) -> None:
        super().__init__(args, returncode, stderr)
        self.next_command = f"gh pr view {pr_number}"


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
    try:
        return subprocess.run(  # noqa: S603
            ["gh", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GhCommandError(["gh", *args], -1, f"timed out after {_GH_TIMEOUT_SECONDS}s: {exc.stderr or ''}") from exc


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


def _current_repo(cwd: Path | None = None) -> tuple[str, str]:
    """Resolves the (owner, name) of the repo `gh` would infer from `cwd` (or the
    process's own cwd when `cwd` is None), the same way a bare `gh pr view <number>`
    with no `-R` does. Used to supply GraphQL's `owner`/`repo` variables, since
    `unresolved_thread_count` -- like `pr_view`/`pr_merge` -- takes only a PR
    number: see the module-level judgment-call note below."""
    result = _run(cwd, "repo", "view", "--json", "nameWithOwner")
    name_with_owner = json.loads(result.stdout)["nameWithOwner"]
    owner, name = name_with_owner.split("/", 1)
    return owner, name


def pr_view(pr_number: int) -> PRStatus:
    """Judgment call: unlike `pr_create`, this takes no `cwd`/repo argument -- a
    bare `gh pr view <number>` infers which repo to hit from the current process's
    working directory's git remote, so callers (Phase 11's orchestrator) are
    expected to already have `cwd` set to the target clone before calling this."""
    result = _run(None, "pr", "view", str(pr_number), "--json", "state,mergeable,reviewDecision")
    data = json.loads(result.stdout)
    return PRStatus(
        state=data["state"],
        mergeable=data["mergeable"],
        review_decision=data.get("reviewDecision") or "",
    )


def unresolved_thread_count(pr_number: int) -> int:
    """Same cwd-inference judgment call as `pr_view`/`pr_merge` (see there): the
    repo to query is resolved from the process's current working directory, not
    passed in.

    Paginates via `reviewThreads`' Relay-style cursor (`pageInfo.hasNextPage`/
    `endCursor`) rather than trusting a single page, since a PR can accumulate more
    threads than one page holds and callers depend on the exact total."""
    owner, repo = _current_repo()
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
            f"repo={repo}",
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


def pr_merge(pr_number: int) -> None:
    """Same cwd-inference judgment call as `pr_view`/`unresolved_thread_count` (see
    there). Squash is the only strategy -- there's no config-driven choice (Phase
    01's `BuildConfig` has no merge-strategy field) -- and a non-zero exit raises
    immediately with no internal retry."""
    argv = ("pr", "merge", str(pr_number), "--squash")
    result = _run_raw(None, *argv)
    if result.returncode != 0:
        raise PRNotMergeableError(["gh", *argv], result.returncode, result.stderr, pr_number)
