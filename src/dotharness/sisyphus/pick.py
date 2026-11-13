"""Ticket picker (FR-9): choose the next work items Sisyphus has not started.

Tracker access goes through the injectable :class:`TrackerClient` and :class:`PullRequestLookup`
protocols; :func:`pick_items` takes both as parameters, so tests use fakes. The real clients here
(the configured code host's ``WorkItems`` and ``ChangeRequests`` for GitHub issues, the Linear GraphQL API) read
credentials only from the inherited environment or a ``token_cmd`` the caller already resolved.
Nothing in this module mutates a tracker (D-8).

Open-PR detection (Q-3) is isolated in :func:`default_open_pr_check`; pass another callable as
``open_pr_check`` to swap it. "Started" means a run record exists for the item.

Imports only Package A modules (never ``harness`` or ``spec_prism_flow``).
"""

# ruff: noqa: TRY003
from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from dotharness.code_host import CodeHost, RepositoryRef
from dotharness.sisyphus.config import SisyphusConfig, WorkItemSource
from dotharness.sisyphus.runrecord import fold_item, record_path
from dotharness.sisyphus.ticketfile import (
    TICKET_SUFFIX,
    parse_front_matter,
    ref_from_filename,
    ticket_dir,
    valid_file_ref,
)

LINEAR_API_URL = "https://api.linear.app/graphql"
GITHUB_OPEN_STATE = "OPEN"


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

    async def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]: ...


class PullRequestLookup(Protocol):
    """GitHub pull-request reads used by the default open-PR check."""

    async def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
        """Pull requests GitHub links to or lists as closing ``issue_number``, any state."""
        ...

    async def open_pr_bodies(self) -> Sequence[str]:
        """Bodies of every open pull request in the repository."""
        ...


OpenPrCheck = Callable[[Candidate, PullRequestLookup], Awaitable[bool]]


def mentions_issue(body: str, number: int) -> bool:
    """True if ``body`` contains ``#<number>`` as a whole reference (``#12`` does not match ``#123``)."""
    return re.search(rf"(?<![\w/#])#{number}(?!\w)", body or "") is not None


async def default_open_pr_check(candidate: Candidate, lookup: PullRequestLookup) -> bool:
    """Q-3 default, the stricter of two signals; either hit excludes the issue.

    (a) an OPEN pull request GitHub links to the issue; (b) an open pull request whose body mentions
    ``#N``. Merged or closed pull requests never exclude.
    """
    if candidate.number is None:
        return False
    if any(pr.state.upper() == GITHUB_OPEN_STATE for pr in await lookup.linked_prs(candidate.number)):
        return True
    return any(mentions_issue(body, candidate.number) for body in await lookup.open_pr_bodies())


def is_started(workspace_dir: str | Path, ref: str, archive_dir: str | Path | None = None) -> bool:
    """A run record exists for ``ref`` in the workspace or in ``archive_dir`` (P3, G4).

    Reads the file system only; never edits a record. ``archive_dir=None`` or a missing directory means
    nothing is archived.
    """
    if record_path(Path(workspace_dir).expanduser(), ref).exists():
        return True
    return archive_dir is not None and record_path(Path(archive_dir).expanduser(), ref).exists()


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _eligible(candidate: Candidate, source: WorkItemSource) -> bool:
    if source.kind == "file":
        return True
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
    if source.kind == "file":
        if not source.dir:
            raise PickError('[source.work_item] dir is required for kind = "file"')
    else:
        if not source.assignee:
            raise PickError("[source.work_item] assignee is required to pick work")
        if source.kind == "linear" and not source.pick_state:
            raise PickError('[source.work_item] pick_state is required for kind = "linear"')
    return source  # NOSONAR python:S3516 - validator: every non-raising path returns `source` by design


async def pick_items(
    source: WorkItemSource | None,
    tracker: TrackerClient,
    lookup: PullRequestLookup | None,
    *,
    workspace_dir: str | Path,
    limit: int,
    open_pr_check: OpenPrCheck = default_open_pr_check,
    archive_dir: str | Path | None = None,
) -> list[Candidate]:
    """Return up to ``limit`` unstarted candidates in pick order (FR-9 step 1).

    Linear and file trackers keep their own order. GitHub issues sort oldest first, then by number ascending (P4)
    and those with an open PR (see :func:`default_open_pr_check`) are skipped; ``lookup`` is required
    for GitHub. Items with a run record are skipped for both trackers, including those whose record sits in ``archive_dir``.
    """
    src = _validate(source)
    if limit <= 0:
        return []
    candidates = [c for c in await tracker.list_candidates(src) if _eligible(c, src)]
    if src.kind == "github":
        if lookup is None:
            raise PickError("a pull-request lookup is required for GitHub issues")
        candidates.sort(key=lambda c: (c.created_at, c.number if c.number is not None else 0))
    return await _take_unstarted(candidates, workspace_dir, limit, src.kind, lookup, open_pr_check, archive_dir)


async def _take_unstarted(
    candidates: Sequence[Candidate],
    workspace_dir: str | Path,
    limit: int,
    kind: str | None,
    lookup: PullRequestLookup | None,
    open_pr_check: OpenPrCheck,
    archive_dir: str | Path | None = None,
) -> list[Candidate]:
    """First ``limit`` candidates with no run record (and, for GitHub, no open PR), de-duplicated by ASCII-folded ref."""
    picked: list[Candidate] = []
    seen: set[str] = set()
    for cand in candidates:
        if fold_item(cand.ref) in seen or is_started(workspace_dir, cand.ref, archive_dir):
            continue
        if lookup is not None and kind == "github" and await open_pr_check(cand, lookup):
            continue
        seen.add(fold_item(cand.ref))
        picked.append(cand)
        if len(picked) >= limit:
            break
    return picked


# --------------------------------------------------------------- real clients
#
# Unsupported-capability policy (docs/sisyphus/configuration.md has the same table). Each site below and in
# ``cli.py``/``orchestrator.py``/``cleanup.py`` names its row; the policy is decided once, here:
#
# | Call site                                              | Capability     | Policy                                    |
# | pick_items listing and linked/open-PR check (github)   | WorkItems      | stop (startup ConfigurationError)         |
# | label removal (finish and ``abandon``)                 | WorkItems      | warn (digest note / one stderr line)      |
# | find_pr, open_pr in the open-PR stage                  | ChangeRequests | stop (CodeHostError maps to C8)           |
# | pr_state in branch cleanup                             | ChangeRequests | degrade (branch kept with a note)         |
# | ``status --check-prs`` state lookup                    | ChangeRequests | warn (``warning:`` line, item shown)      |
# | default-branch lookup for intake                       | Repository     | degrade (any failure returns ``None``)    |


class HostRepository:
    """One lazily resolved :class:`RepositoryRef` for the clone; the consumer contracts below take no ref."""

    def __init__(self, host: CodeHost) -> None:
        self.host = host
        self._ref: RepositoryRef | None = None

    async def ref(self) -> RepositoryRef:
        if self._ref is None:
            self._ref = await self.host.repository.current()
        return self._ref


class LabelRemover(Protocol):
    """Removes one label from one work item. Any failure is raised (the callers decide: warn, never stop)."""

    async def remove_label(self, number: int, label: str) -> None: ...


class HostLabelRemover:
    """:class:`LabelRemover` over ``WorkItems.remove_label``.

    Policy (WorkItems unsupported): warn. The ``UnsupportedCapabilityError`` propagates; the orchestrator records a
    digest note and ``abandon`` prints one stderr line. Removing a label the item does not carry is a no-op; a label
    that does not exist in the repository raises (a configuration error the human must fix).
    """

    def __init__(self, repo: HostRepository) -> None:
        self.repo = repo

    async def remove_label(self, number: int, label: str) -> None:
        await self.repo.host.work_items.remove_label(await self.repo.ref(), number, label)


_HASH_REF = re.compile(r"#(\d+)")


def label_removal_number(config: SisyphusConfig, ref: str) -> int | None:
    """Issue number to remove the pick label from, or ``None`` when no call may be made.

    Only for ``kind = "github"`` with ``pick_label`` set and ``remove_label_when_settled`` on, and a ``#N`` ref.
    """
    wi = config.source.work_item
    if wi is None or wi.kind != "github" or not wi.pick_label or not wi.remove_label_when_settled:
        return None
    match = _HASH_REF.fullmatch(ref)
    return int(match.group(1)) if match else None


class HostIssueTracker:
    """Open issues assigned to ``assignee`` with ``pick_label`` (D-18), read through ``WorkItems.list_open``.

    Policy (WorkItems unsupported): stop. ``default_services`` refuses to start for ``kind = "github"`` on a host
    without WorkItems, so this never runs there. The host's declared list bound replaces the old fixed 100.
    """

    def __init__(self, repo: HostRepository) -> None:
        self.repo = repo

    async def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        assert source.assignee is not None  # noqa: S101
        # ``label=""`` (not None) keeps ``--label ""`` in the argv when no pick_label is set, as before.
        items = await self.repo.host.work_items.list_open(
            await self.repo.ref(), assignee=source.assignee, label=source.pick_label or ""
        )
        return [
            Candidate(
                ref=f"#{item.number}",
                title=item.title,
                number=item.number,
                created_at=item.created_at.isoformat(),
                assignees=tuple(user.login for user in item.assignees),
                labels=item.labels,
                state="open",
            )
            for item in items
        ]


class HostPullRequestLookup:
    """Linked and open pull requests through ``WorkItems.closing_change_requests`` and ``open_change_request_bodies``.

    Policy (WorkItems unsupported): stop, same as :class:`HostIssueTracker`.
    """

    def __init__(self, repo: HostRepository) -> None:
        self.repo = repo

    async def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
        crs = await self.repo.host.work_items.closing_change_requests(await self.repo.ref(), issue_number)
        return [LinkedPr(cr.number, cr.state.name) for cr in crs]

    async def open_pr_bodies(self) -> Sequence[str]:
        crs = await self.repo.host.work_items.open_change_request_bodies(await self.repo.ref())
        return [cr.body or "" for cr in crs]


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

    async def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        assert source.assignee is not None and source.pick_state is not None  # noqa: S101
        field = "email" if "@" in source.assignee else "displayName"
        variables = {"state": source.pick_state, "assignee": {field: {"eq": source.assignee}}}
        body = json.dumps({"query": _LINEAR_QUERY, "variables": variables}).encode()
        try:
            headers = {"Content-Type": "application/json", "Authorization": self.token}
            raw = await asyncio.to_thread(self._post, LINEAR_API_URL, body, headers)
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


def _parse_priority(raw: str | None) -> int:
    """Linear priority 1 to 4; anything else (missing, non-integer, out of range) is 0 (none)."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        return 0
    return value if 1 <= value <= 4 else 0


class FileTracker:
    """Ticket files ``<dir>/<ref>.md`` written by a producer; no credentials and no network.

    Order: Linear priority 1 to 4 first (0 or missing last), then file mtime ascending, then ``ref``.
    Invalid files are skipped with one line on stderr naming the file and the reason.
    """

    async def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        return await asyncio.to_thread(self._list, source)

    def _list(self, source: WorkItemSource) -> Sequence[Candidate]:
        if not source.dir:
            return []
        inbox = ticket_dir(source)
        if not inbox.exists():
            return []
        try:
            paths = sorted(p for p in inbox.iterdir() if p.name.endswith(TICKET_SUFFIX) and p.is_file())
        except OSError as exc:
            raise PickError(f"cannot read ticket directory {inbox}: {exc}") from exc
        rows: list[tuple[int, int, str, Candidate]] = []
        for path in paths:
            try:
                row = self._read(path)
            except (ValueError, OSError) as exc:
                print(f"sisyphus: skipping ticket file {path}: {exc}", file=sys.stderr)
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r[0] or 5, r[1], r[2]))
        return [r[3] for r in rows]

    @staticmethod
    def _read(path: Path) -> tuple[int, int, str, Candidate]:
        fields = parse_front_matter(path.read_text(encoding="utf-8"))
        ref = fields.get("ref", "")
        if not ref:
            raise ValueError("missing ref")
        if not valid_file_ref(ref):
            raise ValueError(f"invalid ref {ref!r}")
        if ref != ref_from_filename(path.name):
            raise ValueError(f"ref {ref!r} does not match the file name")
        mtime_ns = path.stat().st_mtime_ns
        stamp = datetime.fromtimestamp(mtime_ns / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cand = Candidate(ref=ref, title=fields.get("title") or ref, created_at=stamp)
        return _parse_priority(fields.get("priority")), mtime_ns, ref, cand
