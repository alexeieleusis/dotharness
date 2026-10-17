"""Ticket picker (FR-9): choose the next work items Sisyphus has not started.

Tracker access goes through the injectable :class:`TrackerClient` and :class:`PullRequestLookup`
protocols; :func:`pick_items` takes both as parameters, so tests use fakes. The real clients here
(``gh`` for GitHub issues, the Linear GraphQL API) are built from subprocess and ``urllib`` and read
credentials only from the inherited environment or a ``token_cmd`` the caller already resolved.
Nothing in this module mutates a tracker (D-8).

Open-PR detection (Q-3) is isolated in :func:`default_open_pr_check`; pass another callable as
``open_pr_check`` to swap it. "Started" means a run record exists for the item.

Imports only Package A modules (never ``harness`` or ``spec_prism_flow``).
"""

# ruff: noqa: TRY003
from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sisyphus.config import WorkItemSource
from sisyphus.orchestrator import BRANCH_CREATED_KEY, PullRequest, branch_name
from sisyphus.resume import iter_records
from sisyphus.runrecord import record_path
from sisyphus.stages import CI_FIX_STAGE, MERGE_STAGE, OPEN_PR_STAGE
from sisyphus.stoprecord import is_abandoned
from sisyphus.subproc import CommandResult, run_command

LINEAR_API_URL = "https://api.linear.app/graphql"
GITHUB_OPEN_STATE = "OPEN"
# Stages at or after "open-pr": an earlier run may have left a PR open.
_PR_STAGES = (OPEN_PR_STAGE, CI_FIX_STAGE, MERGE_STAGE)


class PickError(Exception):
    """Raised for an unusable pick setup or a tracker call that failed."""


@dataclass(frozen=True)
class Candidate:
    """One work item offered by a tracker.

    ``ref`` is the item reference handed to the orchestrator (a Linear identifier such as ``ENG-12``,
    or ``#<number>`` for a GitHub issue). ``assignees`` holds every identifier the tracker knows for
    the assignee (login, e-mail, display name) and ``labels`` the label names; ``state`` is the
    tracker state name for Linear and ``open`` for an open GitHub issue.
    """

    ref: str
    title: str = ""
    number: int | None = None
    created_at: str = ""
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    state: str = ""


@dataclass(frozen=True)
class LinkedPr:
    """A pull request linked to (or closing) an issue, with its state (``OPEN``, ``MERGED``, ``CLOSED``)."""

    number: int
    state: str


class TrackerClient(Protocol):
    """Read-only tracker access. Linear results come back in the tracker's own priority order."""

    def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]: ...


class PullRequestLookup(Protocol):
    """GitHub pull-request reads used by the default open-PR check."""

    def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
        """Pull requests GitHub links to or lists as closing ``issue_number``, any state."""
        ...

    def open_pr_bodies(self) -> Sequence[str]:
        """Bodies of every open pull request in the repository."""
        ...


OpenPrCheck = Callable[[Candidate, PullRequestLookup], bool]


def mentions_issue(body: str, number: int) -> bool:
    """True if ``body`` contains ``#<number>`` as a whole reference (``#12`` does not match ``#123``)."""
    return re.search(rf"(?<![\w/#])#{number}(?!\w)", body or "") is not None


def default_open_pr_check(candidate: Candidate, lookup: PullRequestLookup) -> bool:
    """Q-3 default, the stricter of two signals; either hit excludes the issue.

    (a) an OPEN pull request GitHub links to the issue; (b) an open pull request whose body mentions
    ``#N``. Merged or closed pull requests never exclude.
    """
    if candidate.number is None:
        return False
    if any(pr.state.upper() == GITHUB_OPEN_STATE for pr in lookup.linked_prs(candidate.number)):
        return True
    return any(mentions_issue(body, candidate.number) for body in lookup.open_pr_bodies())


def is_started(workspace_dir: str | Path, ref: str) -> bool:
    """A run record exists for ``ref`` (P3). Reads the file system only; never edits the record."""
    return record_path(Path(workspace_dir).expanduser(), ref).exists()


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _eligible(candidate: Candidate, source: WorkItemSource) -> bool:
    assert source.assignee is not None  # noqa: S101
    if not any(_same(a, source.assignee) for a in candidate.assignees):
        return False
    if source.kind == "github":
        label = source.pick_label or ""
        return _same(candidate.state or "open", "open") and any(_same(lbl, label) for lbl in candidate.labels)
    return source.pick_state is not None and _same(candidate.state, source.pick_state)


def _validate(source: WorkItemSource | None) -> WorkItemSource:
    if source is None or source.kind is None:
        raise PickError("[source.work_item] with a kind is required to pick work")
    if not source.assignee:
        raise PickError("[source.work_item] assignee is required to pick work")
    if source.kind == "linear" and not source.pick_state:
        raise PickError('[source.work_item] pick_state is required for kind = "linear"')
    return source


def pick_items(
    source: WorkItemSource | None,
    tracker: TrackerClient,
    lookup: PullRequestLookup | None,
    *,
    workspace_dir: str | Path,
    limit: int,
    open_pr_check: OpenPrCheck = default_open_pr_check,
) -> list[Candidate]:
    """Return up to ``limit`` unstarted candidates in pick order (FR-9 step 1).

    Linear keeps the tracker's order. GitHub issues sort oldest first, then by number ascending (P4)
    and those with an open PR (see :func:`default_open_pr_check`) are skipped; ``lookup`` is required
    for GitHub. Items with a run record are skipped for both trackers.
    """
    src = _validate(source)
    if limit <= 0:
        return []
    candidates = [c for c in tracker.list_candidates(src) if _eligible(c, src)]
    if src.kind == "github":
        if lookup is None:
            raise PickError("a pull-request lookup is required for GitHub issues")
        candidates.sort(key=lambda c: (c.created_at, c.number if c.number is not None else 0))
    return _take_unstarted(candidates, workspace_dir, limit, src.kind, lookup, open_pr_check)


def _take_unstarted(
    candidates: Sequence[Candidate],
    workspace_dir: str | Path,
    limit: int,
    kind: str | None,
    lookup: PullRequestLookup | None,
    open_pr_check: OpenPrCheck,
) -> list[Candidate]:
    """First ``limit`` candidates with no run record (and, for GitHub, no open PR), de-duplicated by ref."""
    picked: list[Candidate] = []
    seen: set[str] = set()
    for cand in candidates:
        if cand.ref in seen or is_started(workspace_dir, cand.ref):
            continue
        if lookup is not None and kind == "github" and open_pr_check(cand, lookup):
            continue
        seen.add(cand.ref)
        picked.append(cand)
        if len(picked) >= limit:
            break
    return picked


# ------------------------------------------------- earlier PR awaiting review


@dataclass(frozen=True)
class AwaitingReview:
    """An earlier Sisyphus item whose PR is still open (FR-9 step 4)."""

    item: str
    branch: str
    pr: PullRequest


def find_awaiting_review(
    workspace_dir: str | Path, find_open_pr: Callable[[str], PullRequest | None]
) -> list[AwaitingReview]:
    """Items with a run record that reached the PR stage and whose Sisyphus branch has an open PR.

    ``find_open_pr(branch)`` returns the open PR with that head branch, or ``None``. Records are read
    only; files that are not run records (stop reports, intake sidecars, anything else) are ignored.
    """
    ws = Path(workspace_dir).expanduser()
    found: list[AwaitingReview] = []
    for rec in iter_records(ws):
        # An item stopped at a checkpoint waits for an answer, not a review; it never blocks picking.
        # An abandoned item never blocks picking either (its PR, if any, is the human's to handle).
        if (
            is_abandoned(rec.path)
            or rec.checkpoint is not None
            or not rec.retries.get(BRANCH_CREATED_KEY)
            or rec.stage not in _PR_STAGES
        ):
            continue
        branch = branch_name(rec.item)
        pr = find_open_pr(branch)
        if pr is not None:
            found.append(AwaitingReview(rec.item, branch, pr))
    return found


# --------------------------------------------------------------- real clients


ExecResult = CommandResult


Exec = Callable[[Sequence[str], Path | None, Mapping[str, str] | None], ExecResult]


def default_exec(argv: Sequence[str], cwd: Path | None, env: Mapping[str, str] | None) -> ExecResult:
    """Run ``argv`` with captured text output. ``env`` entries are added to the inherited environment."""
    return run_command(argv, cwd, env)


class GhCli:
    """Thin ``gh`` wrapper shared by the GitHub tracker and pull-request lookup."""

    def __init__(self, cwd: Path | None = None, token: str | None = None, run: Exec = default_exec) -> None:
        self.cwd = cwd
        self.env = {"GH_TOKEN": token} if token else None
        self._run = run

    def run(self, *args: str) -> ExecResult:
        """Run ``gh <args>`` in the configured directory and environment; the caller inspects the result."""
        return self._run(["gh", *args], self.cwd, self.env)

    def json(self, *args: str) -> Any:
        res = self.run(*args)
        if res.returncode != 0:
            raise PickError(f"gh {' '.join(args[:2])} failed (exit {res.returncode}): {res.stderr.strip()[-300:]}")
        try:
            return json.loads(res.stdout or "null")
        except ValueError as exc:
            raise PickError(f"gh {' '.join(args[:2])} returned invalid JSON: {exc}") from exc


class GitHubIssueTracker:
    """Open issues assigned to ``assignee`` with ``pick_label`` (D-18), read through ``gh issue list``."""

    def __init__(self, gh: GhCli, limit: int = 100) -> None:
        self.gh = gh
        self.limit = limit

    def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        assert source.assignee is not None  # noqa: S101
        rows = self.gh.json(
            "issue", "list", "--state", "open", "--assignee", source.assignee, "--label", source.pick_label or "",
            "--limit", str(self.limit), "--json", "number,title,createdAt,assignees,labels",
        )  # fmt: skip
        return [
            Candidate(
                ref=f"#{row['number']}",
                title=row.get("title", ""),
                number=int(row["number"]),
                created_at=row.get("createdAt", ""),
                assignees=tuple(a.get("login", "") for a in row.get("assignees", [])),
                labels=tuple(lbl.get("name", "") for lbl in row.get("labels", [])),
                state="open",
            )
            for row in rows or []
        ]


class GhPullRequestLookup:
    """Linked and open pull requests via ``gh`` (``closedByPullRequestsReferences``, ``gh pr list``)."""

    def __init__(self, gh: GhCli, limit: int = 200) -> None:
        self.gh = gh
        self.limit = limit

    def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
        view = self.gh.json("issue", "view", str(issue_number), "--json", "closedByPullRequestsReferences")
        linked: list[LinkedPr] = []
        for ref in (view or {}).get("closedByPullRequestsReferences", []):
            pr = self.gh.json("pr", "view", str(ref["number"]), "--json", "number,state")
            linked.append(LinkedPr(int(pr["number"]), str(pr["state"])))
        return linked

    def open_pr_bodies(self) -> Sequence[str]:
        rows = self.gh.json("pr", "list", "--state", "open", "--limit", str(self.limit), "--json", "body")
        return [row.get("body") or "" for row in rows or []]


Post = Callable[[str, bytes, Mapping[str, str]], bytes]


def _urlopen_post(url: str, payload: bytes, headers: Mapping[str, str]) -> bytes:
    request = urllib.request.Request(url, data=payload, headers=dict(headers), method="POST")  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read()


_LINEAR_QUERY = """
query($state: String!, $assignee: NullableUserFilter!) {
  issues(first: 50, filter: {state: {name: {eq: $state}}, assignee: $assignee}) {
    nodes { identifier title priority sortOrder createdAt
            state { name } assignee { email displayName name } }
  }
}
"""


class LinearTracker:
    """Issues in ``pick_state`` assigned to ``assignee``, ordered by Linear priority.

    Urgent (1) first through low (4); issues with no priority (0) last; ties keep Linear's manual
    ``sortOrder``. Needs an API key (``LINEAR_API_KEY`` supplied by the caller).
    """

    def __init__(self, token: str, post: Post = _urlopen_post) -> None:
        if not token:
            raise PickError("Linear needs an API key: set LINEAR_API_KEY")
        self.token = token
        self._post = post

    def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        assert source.assignee is not None and source.pick_state is not None  # noqa: S101
        field = "email" if "@" in source.assignee else "displayName"
        variables = {"state": source.pick_state, "assignee": {field: {"eq": source.assignee}}}
        body = json.dumps({"query": _LINEAR_QUERY, "variables": variables}).encode()
        try:
            raw = self._post(LINEAR_API_URL, body, {"Content-Type": "application/json", "Authorization": self.token})
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise PickError(f"Linear request failed: {exc}") from exc
        if not isinstance(data, dict):
            raise PickError(f"Linear returned an unexpected response: {data!r}")
        if data.get("errors"):
            raise PickError(f"Linear returned errors: {data['errors']}")
        try:
            nodes = data["data"]["issues"]["nodes"]
        except (KeyError, TypeError) as exc:
            raise PickError(f"Linear response has no issues: {data!r}") from exc
        if not isinstance(nodes, list):
            raise PickError(f"Linear response has no issues: {data!r}")
        nodes.sort(key=lambda n: ((n.get("priority") or 5), n.get("sortOrder") or 0.0))
        return [
            Candidate(
                ref=n["identifier"],
                title=n.get("title", ""),
                created_at=n.get("createdAt", ""),
                assignees=tuple(v for v in (n.get("assignee") or {}).values() if v),
                state=(n.get("state") or {}).get("name", ""),
            )
            for n in nodes
        ]
