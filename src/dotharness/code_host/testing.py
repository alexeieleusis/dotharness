# A shipped test suite: plain asserts and AssertionError messages are the point.
# ruff: noqa: S101, TRY003
"""In-memory code host and conformance suite.

``InMemoryCodeHost`` serves a consumer test without a real host. ``CodeHostConformance`` is a base class of
behavior tests that every host must pass. A host author subclasses it and overrides ``make_target``.

This module imports no ``pytest``: the suite uses plain ``assert`` and the base class holds ``async def test_...``
coroutines, so whatever runner collects the subclass runs them. It imports nothing from ``vibe_heal``.
Examples use the placeholder token ``<token>``.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

from dotharness.code_host import interfaces
from dotharness.code_host.declarations import Anchor, Capability, HostDeclaration
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    ConfigurationError,
    NoChangeRequestError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.factory import HostRegistration, TokenCmd
from dotharness.code_host.interfaces import CodeHost
from dotharness.code_host.models import (
    ChangeRequest,
    ChangeRequestState,
    Check,
    CheckStatus,
    Comment,
    CommitInfo,
    Credential,
    InlineComment,
    Job,
    RepositoryRef,
    Review,
    ReviewPostResult,
    ReviewState,
    ReviewThread,
    User,
    Workflow,
    WorkItem,
    WorkItemState,
)
from dotharness.code_host.reference import DECLARATION
from dotharness.code_host.version import CODE_HOST_INTERFACE_VERSION

# Sentinel: it must never appear in an error, log or repr.
SECRET = "<token>"  # noqa: S105
NO_CHANGE_REQUEST_TEXT = "No open pull request found for the current branch"
_TRUNCATION_NOTICE = "\n\n_...review body truncated to fit the host size limit._"
_BASE_DATE = "2026-01-01T00:00:00Z"
_T = TypeVar("_T")

# --------------------------------------------------------------------------------------------------------------------
# Seeder protocol and target
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PostedItem:
    """One successful post: a ``review`` (with inline comments) or a top-level ``comment``."""

    kind: str
    body: str
    inline_count: int = 0


class HostSeeder(Protocol):
    """Test-side handle that sets the state of one host and reads back what it received.

    ``InMemoryCodeHost`` is its own seeder. A real host supplies a seeder that stubs its transport.
    """

    def set_available(self, available: bool) -> None: ...

    def set_current_change_request(self, number: int | None, base: str = "main") -> None:
        """Set the change request of the checked-out branch, or None for no change request."""
        ...

    def reject_inline_reviews(self, reason: str) -> None:
        """The host rejects reviews that carry inline comments. A top-level comment is accepted."""
        ...

    def reject_all_posts(self, reason: str) -> None:
        """The host rejects every post."""
        ...

    def fail_posts_with(self, error: Exception) -> None:
        """Every post attempt raises ``error`` (a timeout, an authentication or an unsupported-capability error)."""
        ...

    def posts(self) -> list[PostedItem]:
        """Successful posts, in order."""
        ...

    def post_attempts(self) -> int:
        """All post attempts, successful or not."""
        ...

    def set_default_branch(self, name: str) -> None:
        """The default branch of the target repository."""
        ...

    def add_work_item(
        self,
        repo: RepositoryRef,
        *,
        title: str,
        body: str = "",
        labels: Sequence[str] = (),
        assignees: Sequence[str] = (),
        state: WorkItemState = WorkItemState.OPEN,
        number: int | None = None,
    ) -> WorkItem:
        """Seed a work item. Insertion order is tracker order. ``number=None`` takes the next number."""
        ...

    def add_change_request(
        self,
        repo: RepositoryRef,
        *,
        head_reference: str,
        base_reference: str = "main",
        title: str = "",
        body: str = "",
        state: ChangeRequestState = ChangeRequestState.OPEN,
        is_draft: bool = False,
        author: str = "fake-user",
        head_revision: str | None = None,
        number: int | None = None,
        requested_reviewers: Sequence[str] = (),
    ) -> ChangeRequest:
        """Seed a change request. ``number=None`` takes the next number."""
        ...

    def link_closing(self, repo: RepositoryRef, *, work_item: int, change_request: int) -> None:
        """Record that the change request closes the work item."""
        ...

    def set_user(self, login: str) -> None:
        """The login of the acting user."""
        ...

    def add_commit(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        revision: str,
        message: str | None = None,
        authors: Sequence[str] = (),
    ) -> CommitInfo:
        """Append a commit to the change request. Commits are listed oldest first."""
        ...

    def add_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        author: str,
        state: ReviewState,
        revision: str | None = None,
        body: str = "",
    ) -> Review:
        """Append a review (without inline comments) to the change request."""
        ...

    def add_comment(self, repo: RepositoryRef, number: int, *, author: str, body: str) -> Comment:
        """Append a change-request-level comment."""
        ...

    def add_thread(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        anchor: Anchor | None,
        comments: Sequence[Comment],
        is_resolved: bool = False,
    ) -> ReviewThread:
        """Append a review thread built from the given comments."""
        ...

    def add_check(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        name: str,
        status: CheckStatus,
        required: bool = False,
        job_id: str | None = None,
    ) -> Check:
        """Append a check run. ``job_id`` is the job the check's link names. A pending check has no completion time."""
        ...

    def add_workflow(self, repo: RepositoryRef, *, name: str) -> Workflow:
        """Add an active workflow."""
        ...

    def add_job(self, repo: RepositoryRef, *, job_id: str, failed_step: str | None = None, log: str = "") -> Job:
        """Add a job with its first failed step and its failed log."""
        ...

    def fail_next(self, operation: str, error: CodeHostError) -> None:
        """The next call to ``operation`` (``Interface.method``) raises ``error``, or the failure a host would report.

        A real host's seeder makes its transport fail: a timeout, an authentication failure or any other failure. The
        host maps it to the error type. The outcome is the same for the suite: the call raises a ``CodeHostError``.
        """
        ...


@dataclass(frozen=True)
class ConformanceTarget:
    host: CodeHost
    seeder: HostSeeder
    repository: RepositoryRef = field(default_factory=lambda: RepositoryRef("example/repo"))
    secret: str = ""  # the credential the host was built with. B-SEC-1 requires it to be SECRET.
    # Operations (``Interface.method``) the host declares but does not implement. The suite asserts that each one
    # raises UnsupportedCapabilityError instead of running its behavior: nothing is skipped silently.
    unsupported: frozenset[str] = frozenset()


# --------------------------------------------------------------------------------------------------------------------
# In-memory fake
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One recorded capability call. ``outcome`` is ``"ok"`` or the class name of the error it raised."""

    operation: str
    args: tuple[object, ...]
    kwargs: Mapping[str, object]
    outcome: str


@dataclass
class _Failure:
    error: Exception | None = None
    reject_inline: str | None = None
    reject_all: str | None = None


@dataclass(frozen=True)
class _Injected:
    error: Exception
    apply: bool


class _AbsentCapability:
    """Stand-in for an undeclared capability. Every method raises UnsupportedCapabilityError."""

    def __init__(self, capability: Capability, methods: Sequence[str]) -> None:
        self._capability = capability
        self._methods = frozenset(methods)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_") or name not in self._methods:
            raise AttributeError(name)
        capability = self._capability

        async def _unsupported(*_args: object, **_kwargs: object) -> Any:  # NOSONAR python:S7503 - callers await it
            msg = f"{capability.value}.{name} is not supported by the in-memory code host"
            raise UnsupportedCapabilityError(msg, capability=capability.value)

        return _unsupported


def _public_methods(protocol: type) -> list[str]:
    return [n for n, _ in inspect.getmembers(protocol, inspect.isfunction) if not n.startswith("_")]


# Every capability is implemented by the fake. Selecting a subset is still possible.
_IMPLEMENTED = frozenset(Capability)
_AGENT_ENVIRONMENT_NAMES = ("GITHUB_TOKEN", "GH_TOKEN")
_OPERATIONS = frozenset(
    f"{capability.value}.{method}"
    for capability in Capability
    for method in _public_methods(getattr(interfaces, capability.value))
)
# Operations that change host state. Only these accept ``apply=True`` in error injection.
_WRITES = frozenset({
    "ChangeRequests.create_draft",
    "ChangeRequests.squash_merge",
    "ChangeRequests.request_reviewer",
    "ChangeRequests.approve",
    "ChangeRequests.mark_viewed",
    "Discussion.post_comment",
    "Discussion.post_review",
    "Discussion.reply",
    "WorkItems.remove_label",
})
_EPOCH = datetime.fromisoformat(_BASE_DATE)
# Per repository, per change request number, in insertion order.
_PerChangeRequest = dict[RepositoryRef, dict[int, list[_T]]]


class InMemoryCodeHost:
    """A host that keeps everything in memory. It is its own seeder.

    ``capabilities`` selects the declared subset (default: ``declaration.capabilities`` when a declaration is given,
    else all six capabilities; giving both with different sets raises
    ``ConfigurationError``). An undeclared capability raises ``UnsupportedCapabilityError`` from each method. Limits and anchors are
    the reference host values unless ``declaration`` is given. No network, no subprocess, no clock: timestamps come
    from a counter that adds one second per created object.

    Seeding: ``add_repository``, ``add_work_item``, ``add_change_request``, ``add_commit``, ``add_review``,
    ``add_comment``, ``add_thread``, ``add_check``, ``add_workflow``, ``add_job``, ``link_closing``,
    ``set_default_branch`` (global, not per repository), ``set_repository``, ``set_user``, and the review-flow
    methods. Failure injection: ``fail_next``, ``fail_always``, ``clear_failures``.
    Inspection: ``calls``, ``writes``, ``work_items``, ``change_requests``, ``comments``, ``threads``, ``reviews``,
    ``checks`` (the last four map a repository to a mapping of change request number to a tuple), ``posts``.
    """

    def __init__(
        self,
        *,
        repository: str = "example/repo",
        user: str = "fake-user",
        token: str = SECRET,
        capabilities: frozenset[Capability] | None = None,
        declaration: HostDeclaration | None = None,
    ) -> None:
        if capabilities is not None:
            declared = frozenset(capabilities)
            if declaration is not None and declaration.capabilities != declared:
                msg = "capabilities and declaration.capabilities disagree; pass only one"
                raise ConfigurationError(msg)
        elif declaration is not None:
            declared = declaration.capabilities
        else:
            declared = _IMPLEMENTED
        self._repository = RepositoryRef(repository)
        self._user = user
        self._token = token
        self._default_branch = "main"
        self._available = True
        self._current_change_request: int | None = None
        self._failure = _Failure()
        self._posts: list[PostedItem] = []
        self._attempts = 0
        self._work_items: dict[RepositoryRef, list[WorkItem]] = {}
        self._change_requests: dict[RepositoryRef, list[ChangeRequest]] = {}
        self._links: dict[RepositoryRef, list[tuple[int, int]]] = {}
        self._labels: dict[RepositoryRef, set[str]] = {}
        self._commits: _PerChangeRequest[CommitInfo] = {}
        self._reviews: _PerChangeRequest[Review] = {}
        self._comments: _PerChangeRequest[Comment] = {}
        self._threads: _PerChangeRequest[ReviewThread] = {}
        self._checks: _PerChangeRequest[Check] = {}
        self._required: set[tuple[RepositoryRef, int, str]] = set()
        self._workflows: dict[RepositoryRef, list[Workflow]] = {}
        self._jobs: dict[RepositoryRef, dict[str, tuple[Job, str]]] = {}
        self._viewed: dict[tuple[RepositoryRef, int], dict[str, bool]] = {}
        self._ids = 0
        self._clock = 0
        self._version = 0  # grows with every state change: a call that moves it is a write
        self._next: dict[str, list[_Injected]] = {}
        self._always: dict[str, _Injected] = {}
        self.calls: list[Call] = []
        self._writes: list[Call] = []
        base = declaration or DECLARATION
        self.declaration: HostDeclaration = HostDeclaration(
            capabilities=declared, limits=base.limits, anchors=base.anchors
        )

    # -- state views ---------------------------------------------------------------------------------------------

    @property
    def writes(self) -> list[Call]:
        """The calls that changed state, including injected failures with ``apply=True`` that took effect."""
        return list(self._writes)

    @property
    def work_items(self) -> Mapping[RepositoryRef, tuple[WorkItem, ...]]:
        return MappingProxyType({repo: tuple(items) for repo, items in self._work_items.items()})

    @property
    def change_requests(self) -> Mapping[RepositoryRef, tuple[ChangeRequest, ...]]:
        return MappingProxyType({repo: tuple(items) for repo, items in self._change_requests.items()})

    @property
    def comments(self) -> Mapping[RepositoryRef, Mapping[int, tuple[Comment, ...]]]:
        return _view(self._comments)

    @property
    def threads(self) -> Mapping[RepositoryRef, Mapping[int, tuple[ReviewThread, ...]]]:
        return _view(self._threads)

    @property
    def reviews(self) -> Mapping[RepositoryRef, Mapping[int, tuple[Review, ...]]]:
        return _view(self._reviews)

    @property
    def checks(self) -> Mapping[RepositoryRef, Mapping[int, tuple[Check, ...]]]:
        return _view(self._checks)

    # -- seeder: review flow -------------------------------------------------------------------------------------

    def set_repository(self, identity: str) -> None:
        self._repository = RepositoryRef(identity)

    def set_user(self, login: str) -> None:
        self._user = login

    def set_default_branch(self, name: str) -> None:
        self._default_branch = name

    def set_available(self, available: bool) -> None:
        self._available = available

    def set_current_change_request(self, number: int | None, base: str = "main") -> None:
        """Mark ``number`` as the current branch's change request, stored in the same set ``get`` and ``state`` read.

        An existing change request with that number is kept and only gets the new base; otherwise one is added.
        """
        if number is None:
            self._current_change_request = None
            return
        items = self._change_requests.setdefault(self._repository, [])
        for index, existing in enumerate(items):
            if existing.number == number:
                items[index] = replace(existing, base_reference=base)
                break
        else:
            self.add_change_request(self._repository, head_reference="", base_reference=base, number=number)
        self._current_change_request = number
        self._version += 1

    def reject_inline_reviews(self, reason: str) -> None:
        self._failure = _Failure(reject_inline=reason)

    def reject_all_posts(self, reason: str) -> None:
        self._failure = _Failure(reject_all=reason)

    def fail_posts_with(self, error: Exception) -> None:
        self._failure = _Failure(error=error)

    def require_client(self) -> None:
        """Raise OSError when the host client is unavailable, as the reference host does."""
        if not self._available:
            msg = "The in-memory code host client is not available"
            raise OSError(msg)

    def posts(self) -> list[PostedItem]:
        return list(self._posts)

    def post_attempts(self) -> int:
        return self._attempts

    # -- seeder: work items and change requests ------------------------------------------------------------------

    def _tick(self) -> datetime:
        self._clock += 1
        return _EPOCH + timedelta(seconds=self._clock)

    def _number(self, existing: Sequence[WorkItem | ChangeRequest], number: int | None) -> int:
        taken = {item.number for item in existing}
        if number is None:
            return max(taken, default=0) + 1
        if number in taken:
            msg = f"number {number} is already used"
            raise ValueError(msg)
        return number

    def add_work_item(
        self,
        repo: RepositoryRef,
        *,
        title: str,
        body: str = "",
        labels: Sequence[str] = (),
        assignees: Sequence[str] = (),
        state: WorkItemState = WorkItemState.OPEN,
        number: int | None = None,
    ) -> WorkItem:
        """Add a work item in insertion order, which stands for tracker order. ``number=None`` takes the next one."""
        items = self._work_items.setdefault(repo, [])
        item = WorkItem(
            repository=repo,
            number=self._number(items, number),
            title=title,
            body=body,
            labels=tuple(labels),
            assignees=tuple(User(login) for login in assignees),
            created_at=self._tick(),
            state=state,
        )
        items.append(item)
        self._labels.setdefault(repo, set()).update(labels)
        self._version += 1
        return item

    def add_change_request(
        self,
        repo: RepositoryRef,
        *,
        head_reference: str,
        base_reference: str = "main",
        title: str = "",
        body: str = "",
        state: ChangeRequestState = ChangeRequestState.OPEN,
        is_draft: bool = False,
        author: str = "fake-user",
        head_revision: str | None = None,
        number: int | None = None,
        requested_reviewers: Sequence[str] = (),
    ) -> ChangeRequest:
        """Add a change request in insertion order. ``head_revision=None`` gives ``rev-<number>``."""
        items = self._change_requests.setdefault(repo, [])
        n = self._number(items, number)
        change_request = ChangeRequest(
            repository=repo,
            number=n,
            title=title,
            body=body,
            url=f"https://example.test/{repo.value}/pull/{n}",
            state=state,
            is_draft=is_draft,
            author=User(author),
            head_reference=head_reference,
            head_revision=f"rev-{n}" if head_revision is None else head_revision,
            base_reference=base_reference,
            is_cross_repository=False,
            created_at=self._tick(),
            requested_reviewers=tuple(User(login) for login in requested_reviewers),
        )
        items.append(change_request)
        self._version += 1
        return change_request

    def _next_id(self) -> str:
        self._ids += 1
        return str(self._ids)

    def _require_change_request(self, repo: RepositoryRef, number: int) -> ChangeRequest:
        for change_request in self._change_requests.get(repo, []):
            if change_request.number == number:
                return change_request
        msg = f"change request {number} does not exist"
        raise ValueError(msg)

    def add_repository(self, identity: str, *, default_branch: str = "main", is_current: bool = False) -> RepositoryRef:
        """Add a repository. ``is_current=True`` makes it the answer of ``Repository.current()``.

        The default branch is global (``set_default_branch``), not per repository: ``default_branch`` is applied
        only together with ``is_current=True``.
        """
        repo = RepositoryRef(identity)
        if is_current:
            self._repository = repo
            self._default_branch = default_branch
            self._version += 1
        return repo

    def add_commit(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        revision: str,
        message: str | None = None,
        authors: Sequence[str] = (),
    ) -> CommitInfo:
        """Append a commit to the change request. Commits are listed oldest first."""
        self._require_change_request(repo, number)
        commit = CommitInfo(
            revision=revision, message=message, authors=tuple(User(a) for a in authors), committed_at=self._tick()
        )
        self._commits.setdefault(repo, {}).setdefault(number, []).append(commit)
        self._version += 1
        return commit

    def add_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        author: str,
        state: ReviewState,
        revision: str | None = None,
        body: str = "",
    ) -> Review:
        """Append a review (without inline comments) to the change request."""
        self._require_change_request(repo, number)
        review = Review(
            id=self._next_id(),
            author=User(author),
            state=state,
            body=body,
            submitted_at=self._tick(),
            revision=revision,
            comments=(),
        )
        self._reviews.setdefault(repo, {}).setdefault(number, []).append(review)
        self._version += 1
        return review

    def add_comment(self, repo: RepositoryRef, number: int, *, author: str, body: str) -> Comment:
        """Append a change-request-level comment."""
        self._require_change_request(repo, number)
        comment = self._new_comment(repo, number, User(author), body)
        self._comments.setdefault(repo, {}).setdefault(number, []).append(comment)
        self._version += 1
        return comment

    def _new_comment(self, repo: RepositoryRef, number: int, author: User, body: str) -> Comment:
        comment_id = self._next_id()
        return Comment(
            id=comment_id,
            author=author,
            body=body,
            created_at=self._tick(),
            url=f"https://example.test/{repo.value}/pull/{number}#comment-{comment_id}",
        )

    def add_thread(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        anchor: Anchor | None,
        comments: Sequence[Comment],
        is_resolved: bool = False,
    ) -> ReviewThread:
        """Append a review thread built from the given comments."""
        self._require_change_request(repo, number)
        thread = ReviewThread(
            id=f"thread-{self._next_id()}", is_resolved=is_resolved, anchor=anchor, comments=tuple(comments)
        )
        self._threads.setdefault(repo, {}).setdefault(number, []).append(thread)
        self._version += 1
        return thread

    def add_check(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        name: str,
        status: CheckStatus,
        required: bool = False,
        job_id: str | None = None,
    ) -> Check:
        """Append a check run. A pending check has no completion time."""
        self._require_change_request(repo, number)
        started = self._tick()
        url = f"https://example.test/{repo.value}/runs/{self._next_id()}"
        check = Check(
            name=name,
            status=status,
            details_url=url if job_id is None else f"{url}/job/{job_id}",
            workflow_name=None,
            started_at=started,
            completed_at=None if status is CheckStatus.PENDING else started + timedelta(seconds=1),
            job_id=job_id,
        )
        self._checks.setdefault(repo, {}).setdefault(number, []).append(check)
        if required:
            self._required.add((repo, number, name))
        self._version += 1
        return check

    def add_workflow(self, repo: RepositoryRef, *, name: str) -> Workflow:
        workflow = Workflow(id=self._next_id(), name=name)
        self._workflows.setdefault(repo, []).append(workflow)
        self._version += 1
        return workflow

    def add_job(self, repo: RepositoryRef, *, job_id: str, failed_step: str | None = None, log: str = "") -> Job:
        job = Job(id=job_id, failed_step=failed_step)
        self._jobs.setdefault(repo, {})[job_id] = (job, log)
        self._version += 1
        return job

    def link_closing(self, repo: RepositoryRef, *, work_item: int, change_request: int) -> None:
        """Record that the change request closes the work item. Both must exist."""
        if not any(w.number == work_item for w in self._work_items.get(repo, [])):
            msg = f"work item {work_item} does not exist"
            raise ValueError(msg)
        if not any(c.number == change_request for c in self._change_requests.get(repo, [])):
            msg = f"change request {change_request} does not exist"
            raise ValueError(msg)
        self._links.setdefault(repo, []).append((work_item, change_request))
        self._version += 1

    # -- failure injection and inspection ------------------------------------------------------------------------

    def _check_injection(self, operation: str, *, apply: bool) -> None:
        if operation != "*" and operation not in _OPERATIONS:
            msg = f"unknown operation {operation!r}: write Interface.method or '*'"
            raise ValueError(msg)
        if apply and operation not in _WRITES:
            msg = f"apply=True is only for write operations, not {operation!r}"
            raise ValueError(msg)

    def fail_next(self, operation: str, error: CodeHostError, *, apply: bool = False) -> None:
        """The next call to ``operation`` (``Interface.method`` or ``"*"``) raises ``error``, then behavior is normal.

        ``apply=True`` (writes only) applies the write to the state first, then raises: "timed out but took effect".
        """
        self._check_injection(operation, apply=apply)
        self._next.setdefault(operation, []).append(_Injected(error, apply))

    def fail_always(self, operation: str, error: CodeHostError, *, apply: bool = False) -> None:
        """Every call to ``operation`` raises ``error`` until ``clear_failures``."""
        self._check_injection(operation, apply=apply)
        self._always[operation] = _Injected(error, apply)

    def clear_failures(self) -> None:
        self._failure = _Failure()
        self._next.clear()
        self._always.clear()

    def _injection(self, operation: str) -> _Injected | None:
        for key in (operation, "*"):
            queue = self._next.get(key)
            if queue:
                return queue.pop(0)
        for key in (operation, "*"):
            if key in self._always:
                return self._always[key]
        return None

    def _record(self, operation: str, args: tuple[object, ...], kwargs: Mapping[str, object], outcome: str) -> Call:
        call = Call(operation, args, dict(kwargs), outcome)
        self.calls.append(call)
        return call

    def _invoke(
        self,
        operation: str,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
        action: Callable[[], _T],
        *,
        injectable: bool = True,
    ) -> _T:
        """Run one capability call: apply injected failures, record the call, track whether it wrote."""
        injected = self._injection(operation) if injectable else None
        if injected is not None and not (injected.apply and operation in _WRITES):
            self._record(operation, args, kwargs, type(injected.error).__name__)
            raise injected.error
        before = self._version
        try:
            result = action()
        except Exception as exc:
            # An injection that was selected always wins: the caller asked for this error, even if the action failed.
            surfaced = exc if injected is None else injected.error
            call = self._record(operation, args, kwargs, type(surfaced).__name__)
            if self._version != before:
                self._writes.append(call)
            if injected is None:
                raise
            raise injected.error from exc
        outcome = "ok" if injected is None else type(injected.error).__name__
        call = self._record(operation, args, kwargs, outcome)
        if self._version != before:
            self._writes.append(call)
        if injected is not None:
            raise injected.error
        return result

    # -- bundle --------------------------------------------------------------------------------------------------

    def code_host(self) -> CodeHost:
        declared = self.declaration.capabilities

        def facet(capability: Capability, real: object) -> Any:
            if capability in declared:
                return real
            return _AbsentCapability(capability, _public_methods(getattr(interfaces, capability.value)))

        return CodeHost(
            session=facet(Capability.SESSION, _FakeSession(self)),
            repository=facet(Capability.REPOSITORY, _FakeRepository(self)),
            change_requests=facet(Capability.CHANGE_REQUESTS, _FakeChangeRequests(self)),
            discussion=facet(Capability.DISCUSSION, _FakeDiscussion(self)),
            checks=facet(Capability.CHECKS, _FakeChecks(self)),
            work_items=facet(Capability.WORK_ITEMS, _FakeWorkItems(self)),
            declaration=self.declaration,
        )

    def target(self) -> ConformanceTarget:
        return ConformanceTarget(host=self.code_host(), seeder=self, repository=self._repository, secret=self._token)

    def _bound(self, operation: str) -> int | None:
        bounds = self.declaration.limits.list_bounds
        return bounds.per_operation.get(operation, bounds.default)


def _view(store: _PerChangeRequest[_T]) -> Mapping[RepositoryRef, Mapping[int, tuple[_T, ...]]]:
    return MappingProxyType({
        repo: MappingProxyType({number: tuple(items) for number, items in by_number.items()})
        for repo, by_number in store.items()
    })


class _Facet:
    def __init__(self, host: InMemoryCodeHost) -> None:
        self._host = host

    def _do(
        self,
        operation: str,
        action: Callable[[], _T],
        *args: object,
        injectable: bool = True,
        **kwargs: object,
    ) -> _T:
        return self._host._invoke(operation, args, kwargs, action, injectable=injectable)


class _FakeSession(_Facet):
    async def available(self) -> bool:  # NOSONAR python:S7503 - implements an async protocol
        # "Never raises": an injected failure does not apply to it.
        return self._do("Session.available", lambda: self._host._available, injectable=False)

    async def credential(self) -> Credential:  # NOSONAR python:S7503 - implements an async protocol
        return self._do("Session.credential", lambda: Credential(token=self._host._token))

    async def current_user(self) -> str:  # NOSONAR python:S7503 - implements an async protocol
        return self._do("Session.current_user", lambda: self._host._user)

    async def agent_environment_names(self) -> Sequence[str]:  # NOSONAR python:S7503 - implements an async protocol
        return self._do("Session.agent_environment_names", lambda: _AGENT_ENVIRONMENT_NAMES)

    async def agent_environment(  # NOSONAR python:S7503 - implements an async protocol
        self, base: Mapping[str, str] | None = None
    ) -> Mapping[str, str]:
        def action() -> Mapping[str, str]:
            # Base first, so a credential variable wins over a base entry of the same name.
            return {**(base or {}), **dict.fromkeys(_AGENT_ENVIRONMENT_NAMES, self._host._token)}

        return self._do("Session.agent_environment", action, base)


class _FakeRepository(_Facet):
    async def current(self) -> RepositoryRef:  # NOSONAR python:S7503 - implements an async protocol
        def action() -> RepositoryRef:
            if not self._host._repository.value:
                msg = "No repository is marked as the current one"
                raise ConfigurationError(msg)
            return self._host._repository

        return self._do("Repository.current", action)

    async def resolve(self, identity: str) -> RepositoryRef:  # NOSONAR python:S7503 - implements an async protocol
        def action() -> RepositoryRef:
            if not identity:
                msg = "Repository identity is empty"
                raise ConfigurationError(msg)
            return RepositoryRef(identity)

        return self._do("Repository.resolve", action, identity)

    async def default_branch(self, repo: RepositoryRef) -> str:  # NOSONAR python:S7503 - implements an async protocol
        return self._do("Repository.default_branch", lambda: self._host._default_branch, repo)

    async def display_name(self, repo: RepositoryRef) -> str:  # NOSONAR python:S7503 - implements an async protocol
        return self._do("Repository.display_name", lambda: repo.value, repo)


class _FakeChangeRequests(_Facet):
    def _find(self, repo: RepositoryRef, number: int) -> ChangeRequest:
        for change_request in self._host._change_requests.get(repo, []):
            if change_request.number == number:
                return change_request
        msg = f"change request {number} not found"
        raise CodeHostError(msg)

    async def for_current_branch(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef
    ) -> ChangeRequest:
        def action() -> ChangeRequest:
            self._host.require_client()
            current = self._host._current_change_request
            if current is None:
                raise NoChangeRequestError(NO_CHANGE_REQUEST_TEXT)
            try:
                return self._find(repo, current)
            except CodeHostError:
                raise NoChangeRequestError(NO_CHANGE_REQUEST_TEXT) from None

        return self._do("ChangeRequests.for_current_branch", action, repo)

    async def get(self, repo: RepositoryRef, number: int) -> ChangeRequest:  # NOSONAR python:S7503 - async protocol
        return self._do("ChangeRequests.get", lambda: self._find(repo, number), repo, number)

    async def state(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> ChangeRequestState:
        return self._do("ChangeRequests.state", lambda: self._find(repo, number).state, repo, number)

    async def find_by_branch(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, branch: str, *, state: ChangeRequestState | None = ChangeRequestState.OPEN
    ) -> Sequence[ChangeRequest]:
        def action() -> list[ChangeRequest]:
            matches = [
                c
                for c in self._host._change_requests.get(repo, [])
                if c.head_reference == branch and (state is None or c.state is state)
            ]
            return matches[::-1]  # the most recent first

        return self._do("ChangeRequests.find_by_branch", action, repo, branch, state=state)

    async def create_draft(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str
    ) -> ChangeRequest:
        def action() -> ChangeRequest:
            for existing in self._host._change_requests.get(repo, []):
                if (
                    existing.state is ChangeRequestState.OPEN
                    and existing.head_reference == head
                    and existing.base_reference == base
                ):
                    msg = f"a change request for {head} into {base} already exists"
                    raise CodeHostError(msg)
            return self._host.add_change_request(
                repo,
                head_reference=head,
                base_reference=base,
                title=title,
                body=body,
                is_draft=True,
                author=self._host._user,
            )

        return self._do("ChangeRequests.create_draft", action, repo, head=head, base=base, title=title, body=body)

    async def list(  # NOSONAR python:S7503 - implements an async protocol
        self,
        repo: RepositoryRef,
        *,
        state: ChangeRequestState = ChangeRequestState.OPEN,
        author: str | None = None,
        assignee: str | None = None,
        requested_reviewer: str | None = None,
    ) -> Sequence[ChangeRequest]:
        def action() -> list[ChangeRequest]:
            # Filters are logins. An empty string means "no filter". Change requests carry no assignees: a login
            # filter on assignee matches nothing.
            found = [
                c
                for c in self._host._change_requests.get(repo, [])
                if c.state is state
                and (not author or (c.author is not None and c.author.login == author))
                and (not requested_reviewer or requested_reviewer in {u.login for u in c.requested_reviewers})
            ]
            if assignee:
                found = []
            bound = self._host._bound("ChangeRequests.list")
            return found if bound is None else found[:bound]

        return self._do(
            "ChangeRequests.list",
            action,
            repo,
            state=state,
            author=author,
            assignee=assignee,
            requested_reviewer=requested_reviewer,
        )

    async def find_by_commit(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, revision: str
    ) -> Sequence[ChangeRequest]:
        def action() -> list[ChangeRequest]:
            commits = self._host._commits.get(repo, {})
            return [
                c
                for c in self._host._change_requests.get(repo, [])
                if c.head_revision == revision or any(k.revision == revision for k in commits.get(c.number, []))
            ]

        return self._do("ChangeRequests.find_by_commit", action, repo, revision)

    async def commits(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> Sequence[CommitInfo]:
        def action() -> list[CommitInfo]:
            self._find(repo, number)
            return list(self._host._commits.get(repo, {}).get(number, []))

        return self._do("ChangeRequests.commits", action, repo, number)

    async def reviews(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> Sequence[Review]:
        def action() -> list[Review]:
            self._find(repo, number)
            return list(self._host._reviews.get(repo, {}).get(number, []))

        return self._do("ChangeRequests.reviews", action, repo, number)

    def _replace(self, repo: RepositoryRef, updated: ChangeRequest) -> None:
        items = self._host._change_requests[repo]
        index = next(i for i, c in enumerate(items) if c.number == updated.number)
        items[index] = updated
        self._host._version += 1

    async def squash_merge(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, *, expected_head: str
    ) -> None:
        def action() -> None:
            change_request = self._find(repo, number)
            if change_request.head_revision != expected_head:
                msg = f"head revision of change request {number} is not {expected_head}"
                raise CodeHostError(msg)
            if change_request.state is not ChangeRequestState.OPEN:
                msg = f"change request {number} is not open"
                raise CodeHostError(msg)
            merged = replace(change_request, state=ChangeRequestState.MERGED)
            self._replace(repo, merged)  # NOSONAR python:S5655 - dataclasses.replace returns a ChangeRequest

        self._do("ChangeRequests.squash_merge", action, repo, number, expected_head=expected_head)

    async def request_reviewer(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, login: str
    ) -> None:
        def action() -> None:
            change_request = self._find(repo, number)
            if login not in {u.login for u in change_request.requested_reviewers}:
                reviewers = (*change_request.requested_reviewers, User(login))
                updated = replace(change_request, requested_reviewers=reviewers)
                self._replace(repo, updated)  # NOSONAR python:S5655 - dataclasses.replace returns a ChangeRequest

        self._do("ChangeRequests.request_reviewer", action, repo, number, login)

    async def approve(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, *, head_revision: str
    ) -> None:
        def action() -> None:
            self._find(repo, number)
            self._host.add_review(
                repo, number, author=self._host._user, state=ReviewState.APPROVED, revision=head_revision
            )

        self._do("ChangeRequests.approve", action, repo, number, head_revision=head_revision)

    async def viewed_files(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> Mapping[str, bool]:
        def action() -> Mapping[str, bool]:
            self._find(repo, number)
            return dict(self._host._viewed.get((repo, number), {}))

        return self._do("ChangeRequests.viewed_files", action, repo, number)

    async def mark_viewed(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, path: str
    ) -> None:
        def action() -> None:
            self._find(repo, number)
            viewed = self._host._viewed.setdefault((repo, number), {})
            if not viewed.get(path, False):  # marking a viewed file again is a no-op
                viewed[path] = True
                self._host._version += 1

        self._do("ChangeRequests.mark_viewed", action, repo, number, path)


class _FakeWorkItems(_Facet):
    async def get(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> WorkItem | None:
        def action() -> WorkItem | None:
            return next((w for w in self._host._work_items.get(repo, []) if w.number == number), None)

        return self._do("WorkItems.get", action, repo, number)

    async def list_open(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, *, assignee: str | None = None, label: str | None = None
    ) -> Sequence[WorkItem]:
        def action() -> list[WorkItem]:
            # An empty string means "no filter", as a consumer passes label="" when it has no label to filter on.
            found = [
                w
                for w in self._host._work_items.get(repo, [])
                if w.state is WorkItemState.OPEN
                and (not assignee or assignee in {u.login for u in w.assignees})
                and (not label or label in w.labels)
            ]
            bound = self._host._bound("WorkItems.list_open")
            return found if bound is None else found[:bound]

        return self._do("WorkItems.list_open", action, repo, assignee=assignee, label=label)

    async def remove_label(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, label: str
    ) -> None:
        def action() -> None:
            items = self._host._work_items.get(repo, [])
            index = next((i for i, w in enumerate(items) if w.number == number), None)
            if index is None:
                msg = f"work item {number} not found"
                raise CodeHostError(msg)
            if label not in self._host._labels.get(repo, set()):
                msg = f"label {label!r} does not exist in the repository"
                raise CodeHostError(msg)
            item = items[index]
            if label in item.labels:  # a label the item does not carry is a no-op
                items[index] = replace(item, labels=tuple(x for x in item.labels if x != label))
                self._host._version += 1

        self._do("WorkItems.remove_label", action, repo, number, label)

    async def closing_change_requests(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> Sequence[ChangeRequest]:
        def action() -> list[ChangeRequest]:
            by_number = {c.number: c for c in self._host._change_requests.get(repo, [])}
            return [by_number[cr] for wi, cr in self._host._links.get(repo, []) if wi == number]

        return self._do("WorkItems.closing_change_requests", action, repo, number)

    async def open_change_request_bodies(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef
    ) -> Sequence[ChangeRequest]:
        def action() -> list[ChangeRequest]:
            found = [c for c in self._host._change_requests.get(repo, []) if c.state is ChangeRequestState.OPEN]
            bound = self._host._bound("WorkItems.open_change_request_bodies")
            return found if bound is None else found[:bound]

        return self._do("WorkItems.open_change_request_bodies", action, repo)

    async def linked_work_items(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> Sequence[WorkItem]:
        def action() -> list[WorkItem]:
            by_number = {w.number: w for w in self._host._work_items.get(repo, [])}
            return [by_number[wi] for wi, cr in self._host._links.get(repo, []) if cr == number]

        return self._do("WorkItems.linked_work_items", action, repo, number)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE


class _FakeDiscussion(_Facet):
    def _attempt(self, *, inline: bool) -> str | None:
        """Count one post attempt. Raise an injected error. Return a rejection reason, or None to accept."""
        host = self._host
        host._attempts += 1
        failure = host._failure
        if failure.error is not None:
            raise failure.error
        if failure.reject_all is not None:
            return failure.reject_all
        if inline and failure.reject_inline is not None:
            return failure.reject_inline
        return None

    async def post_review(  # NOSONAR python:S7503 - implements an async protocol
        self,
        repo: RepositoryRef,
        number: int,
        *,
        body: str,
        inline_comments: Sequence[InlineComment],
        head_revision: str | None = None,
        fallback_body: str | None = None,
    ) -> ReviewPostResult:
        def action() -> ReviewPostResult:
            self._host.require_client()
            limits = self._host.declaration.limits
            text = _truncate(body, limits.max_review_body_chars)
            inline = inline_comments[: limits.max_inline_comments_per_review]
            reason = self._attempt(inline=bool(inline))
            if reason is None:
                self._host._posts.append(PostedItem("review", text, len(inline)))
                self._host._version += 1
                return ReviewPostResult(fell_back=False, review=None, comment=None)
            fallback = text if fallback_body is None else _truncate(fallback_body, limits.max_review_body_chars)
            fallback_reason = self._attempt(inline=False)
            if fallback_reason is not None:
                msg = f"Review posting failed: {fallback_reason}"
                raise CodeHostError(msg)
            self._host._posts.append(PostedItem("comment", fallback))
            self._host._version += 1
            return ReviewPostResult(fell_back=True, review=None, comment=None, reason=reason)

        return self._do(
            "Discussion.post_review",
            action,
            repo,
            number,
            body=body,
            inline_comments=inline_comments,
            head_revision=head_revision,
            fallback_body=fallback_body,
        )

    async def comments(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> Sequence[Comment]:
        def action() -> list[Comment]:
            return list(self._host._comments.get(repo, {}).get(number, []))

        return self._do("Discussion.comments", action, repo, number)

    async def review_threads(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int
    ) -> Sequence[ReviewThread]:
        def action() -> list[ReviewThread]:
            return list(self._host._threads.get(repo, {}).get(number, []))

        return self._do("Discussion.review_threads", action, repo, number)

    async def post_comment(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, body: str
    ) -> Comment:
        def action() -> Comment:
            self._host.require_client()
            comment = self._host._new_comment(repo, number, User(self._host._user), body)
            self._host._comments.setdefault(repo, {}).setdefault(number, []).append(comment)
            self._host._version += 1
            return comment

        return self._do("Discussion.post_comment", action, repo, number, body)

    async def reply(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, thread_id: str, body: str
    ) -> Comment:
        def action() -> Comment:
            threads = self._host._threads.get(repo, {}).get(number, [])
            index = next((i for i, t in enumerate(threads) if t.id == thread_id), None)
            if index is None:
                msg = f"review thread {thread_id!r} not found"
                raise CodeHostError(msg)
            comment = self._host._new_comment(repo, number, User(self._host._user), body)
            thread = threads[index]
            threads[index] = replace(thread, comments=(*thread.comments, comment))
            self._host._version += 1
            return comment

        return self._do("Discussion.reply", action, repo, number, thread_id, body)


class _FakeChecks(_Facet):
    async def list_checks(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef, number: int, *, required_only: bool = False
    ) -> Sequence[Check]:
        def action() -> list[Check]:
            found = list(self._host._checks.get(repo, {}).get(number, []))
            if required_only:
                found = [c for c in found if (repo, number, c.name) in self._host._required]
            return found

        return self._do("Checks.list_checks", action, repo, number, required_only=required_only)

    async def list_workflows(  # NOSONAR python:S7503 - implements an async protocol
        self, repo: RepositoryRef
    ) -> Sequence[Workflow]:
        return self._do("Checks.list_workflows", lambda: list(self._host._workflows.get(repo, [])), repo)

    async def failed_log(self, repo: RepositoryRef, check: Check) -> str:  # NOSONAR python:S7503 - async protocol
        def action() -> str:
            if check.job_id is None:
                msg = f"check {check.name!r} has no job to read a log from"
                raise CodeHostError(msg)
            found = self._host._jobs.get(repo, {}).get(check.job_id)
            if found is None:
                msg = f"job {check.job_id!r} not found"
                raise CodeHostError(msg)
            return found[1]

        return self._do("Checks.failed_log", action, repo, check)

    async def get_job(self, repo: RepositoryRef, job_id: str) -> Job:  # NOSONAR python:S7503 - async protocol
        def action() -> Job:
            found = self._host._jobs.get(repo, {}).get(job_id)
            if found is None:
                msg = f"job {job_id!r} not found"
                raise CodeHostError(msg)
            return found[0]

        return self._do("Checks.get_job", action, repo, job_id)


@dataclass(frozen=True)
class FakeOptions:
    """Options of the fake registration."""

    repository: str = "example/repo"
    user: str = "fake-user"


def _create_fake(options: FakeOptions, token_cmd: TokenCmd, working_directory: Path | None = None) -> CodeHost:
    """Factory of ``FAKE_REGISTRATION``. The fake runs no command, so the token command and directory are unused."""
    return InMemoryCodeHost(repository=options.repository, user=options.user).code_host()


FAKE_REGISTRATION = HostRegistration(
    interface_version=CODE_HOST_INTERFACE_VERSION,
    options_model=FakeOptions,
    factory=_create_fake,
)


# --------------------------------------------------------------------------------------------------------------------
# Conformance suite
# --------------------------------------------------------------------------------------------------------------------


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(record.getMessage())
        if record.exc_text:
            self.text.append(record.exc_text)


def _chain(exc: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _comment(comment_id: str, author: str, body: str) -> Comment:
    """A comment for ``HostSeeder.add_thread``."""
    return Comment(id=comment_id, author=User(author), body=body, created_at=_EPOCH, url=None)


def _dummy_args(method: Callable[..., Any]) -> tuple[list[Any], dict[str, Any]]:
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for p in inspect.signature(method).parameters.values():
        if p.name == "self" or p.default is not inspect.Parameter.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        dummies: dict[str, Any] = {"repo": RepositoryRef("example/repo"), "number": 1}
        value = dummies.get(p.name, "x")
        if p.kind is p.KEYWORD_ONLY:
            kwargs[p.name] = value
        else:
            args.append(value)
    return args, kwargs


class CodeHostConformance:
    """Behavior tests every host must pass.

    Subclass it and override ``make_target``. Behavior ids B-NCR, B-REV, B-LIM, B-ERR, B-SEC, B-SES-3, B-REP-3,
    B-CR-1, B-CR-2, B-CR-5 and B-WI-1..5 are listed in docs/design/code-host.md, "Behaviors asserted". A group runs
    only for a declared capability (B-ERR-4 covers an undeclared one). A declared operation the host does not
    implement goes in ``ConformanceTarget.unsupported``: the suite then asserts that it raises.
    """

    async def make_target(self) -> ConformanceTarget:  # NOSONAR python:S7503 - subclasses await their host setup
        """Return a fresh host and its seeder for one test."""
        raise NotImplementedError

    async def _post(
        self,
        target: ConformanceTarget,
        *,
        fallback_body: str | None = None,
        inline: int = 1,
        body: str = "given body",
    ) -> ReviewPostResult:
        comments = [InlineComment(f"c{i}", Anchor("src/a.py", i + 1, "RIGHT")) for i in range(inline)]
        return await target.host.discussion.post_review(
            target.repository, 7, body=body, inline_comments=comments, fallback_body=fallback_body
        )

    # -- session and change request ------------------------------------------------------------------------------

    async def test_available_returns_bool(self) -> None:
        """B-NCR-1: available() returns a bool and does not raise, available or not."""
        target = await self.make_target()
        assert await target.host.session.available() is True
        target.seeder.set_available(False)
        assert await target.host.session.available() is False

    async def test_for_current_branch_returns_number_and_base(self) -> None:
        """B-NCR-2: for_current_branch returns the change request number and base."""
        target = await self.make_target()
        target.seeder.set_current_change_request(73, "release")
        cr = await target.host.change_requests.for_current_branch(target.repository)
        assert cr.number == 73
        assert cr.base_reference == "release"

    async def test_no_change_request_error_text(self) -> None:
        """B-NCR-3: no change request raises NoChangeRequestError (a CodeHostError) with the standard text."""
        target = await self.make_target()
        target.seeder.set_current_change_request(None)
        try:
            await target.host.change_requests.for_current_branch(target.repository)
        except NoChangeRequestError as exc:
            assert isinstance(exc, CodeHostError)
            assert NO_CHANGE_REQUEST_TEXT in str(exc)
        else:
            raise AssertionError("NoChangeRequestError not raised")

    async def test_unavailable_client_raises_oserror(self) -> None:
        """B-NCR-4: an unavailable host client raises OSError from for_current_branch and post_review."""
        target = await self.make_target()
        target.seeder.set_available(False)
        for call in (
            target.host.change_requests.for_current_branch(target.repository),
            self._post(target),
        ):
            try:
                await call
            except OSError:
                pass
            else:
                raise AssertionError("OSError not raised")
        assert target.seeder.posts() == []

    # -- post_review ---------------------------------------------------------------------------------------------

    async def test_post_review_without_fallback(self) -> None:
        """B-REV-1: an accepted review posts body and inline comments as given; fell_back false; reason None."""
        target = await self.make_target()
        result = await self._post(target, inline=2)
        assert result.fell_back is False
        assert result.reason is None
        assert target.seeder.posts() == [PostedItem("review", "given body", 2)]

    async def test_post_review_with_fallback(self) -> None:
        """B-REV-2: a rejected review falls back to one top-level comment; fell_back true; reason has the cause."""
        target = await self.make_target()
        target.seeder.reject_inline_reviews("line could not be resolved")
        result = await self._post(target)
        assert result.fell_back is True
        assert result.reason is not None
        assert "line could not be resolved" in result.reason
        posts = target.seeder.posts()
        assert [p.kind for p in posts] == ["comment"]
        assert posts[0].inline_count == 0
        assert posts[0].body == "given body"  # no fallback_body: the given body is the comment

    async def test_fallback_body_posted(self) -> None:
        """B-REV-3: with a fallback_body the fallback comment is that text."""
        target = await self.make_target()
        target.seeder.reject_inline_reviews("rejected")
        result = await self._post(target, fallback_body="fallback text")
        assert result.fell_back is True
        (post,) = target.seeder.posts()
        assert post == PostedItem("comment", "fallback text")

    async def test_reason_only_when_fell_back(self) -> None:
        """B-REV-4: reason is set if and only if fell_back is true."""
        target = await self.make_target()
        ok = await self._post(target)
        assert ok.fell_back is False
        assert ok.reason is None
        target.seeder.reject_inline_reviews("nope")
        fell = await self._post(target)
        assert fell.fell_back is True
        assert fell.reason

    async def test_no_fallback_after_timeout_auth_or_unsupported(self) -> None:
        """B-REV-5: CodeHostTimeoutError, AuthenticationError and UnsupportedCapabilityError propagate without fallback."""
        errors: list[Callable[[], Exception]] = [
            lambda: CodeHostTimeoutError("post timed out", write=True),
            lambda: AuthenticationError("not authenticated"),
            lambda: UnsupportedCapabilityError("not supported", capability="Discussion"),
        ]
        for make_error in errors:
            target = await self.make_target()
            expected = make_error()
            target.seeder.fail_posts_with(expected)
            try:
                await self._post(target)
            except CodeHostError as exc:
                assert type(exc) is type(expected)
            else:
                raise AssertionError(f"{type(expected).__name__} not raised")
            assert target.seeder.post_attempts() == 1, type(expected).__name__
            assert target.seeder.posts() == []

    async def test_single_fallback_only(self) -> None:
        """B-REV-6: when the fallback also fails there is no third attempt; a CodeHostError propagates."""
        target = await self.make_target()
        target.seeder.reject_all_posts("everything rejected")
        try:
            await self._post(target)
        except CodeHostError as exc:
            assert not isinstance(exc, (CodeHostTimeoutError, AuthenticationError, UnsupportedCapabilityError))
        else:
            raise AssertionError("CodeHostError not raised")
        assert target.seeder.post_attempts() == 2
        assert target.seeder.posts() == []

    # -- preview and limits --------------------------------------------------------------------------------------

    async def test_declared_limits(self) -> None:
        """B-LIM-1: the host declares 50 inline comments and 60000 body characters."""
        target = await self.make_target()
        limits = target.host.declaration.limits
        assert limits.max_inline_comments_per_review == 50
        assert limits.max_review_body_chars == 60_000

    async def test_inline_comments_capped(self) -> None:
        """B-LIM-2: a review posts at most the declared number of inline comments."""
        target = await self.make_target()
        cap = target.host.declaration.limits.max_inline_comments_per_review
        await self._post(target, inline=cap + 10)
        (post,) = target.seeder.posts()
        assert post.inline_count == cap

    async def test_body_truncated_with_notice(self) -> None:
        """B-LIM-3: a body or fallback body above the declared limit is cut to the limit and ends with a notice."""
        target = await self.make_target()
        limit = target.host.declaration.limits.max_review_body_chars
        await self._post(target, body="x" * (limit + 5_000))
        (post,) = target.seeder.posts()
        assert len(post.body) <= limit
        assert "truncated" in post.body[-200:].lower()
        target = await self.make_target()
        target.seeder.reject_inline_reviews("rejected")
        await self._post(target, fallback_body="y" * (limit + 5_000))
        (post,) = target.seeder.posts()
        assert len(post.body) <= limit
        assert "truncated" in post.body[-200:].lower()

    # -- helpers for the per-capability groups -------------------------------------------------------------------

    @staticmethod
    def _declares(target: ConformanceTarget, capability: Capability) -> bool:
        """False when the capability is undeclared. B-ERR-4 covers it instead of its group."""
        return capability in target.host.declaration.capabilities

    @staticmethod
    async def _unsupported_op(target: ConformanceTarget, operation: str, make_call: Callable[[], Any]) -> bool:
        """When the target lists ``operation`` as unsupported, assert that it raises and return True."""
        if operation not in target.unsupported:
            return False
        try:
            await make_call()
        except UnsupportedCapabilityError as exc:
            assert exc.capability == operation.split(".")[0], operation
        else:
            raise AssertionError(f"{operation} is listed as unsupported but did not raise")
        return True

    # -- session and repository ----------------------------------------------------------------------------------

    async def test_credential_repr_hides_token(self) -> None:
        """B-SES-3: credential() returns a Credential whose repr does not contain the token."""
        target = await self.make_target()
        if not self._declares(target, Capability.SESSION):
            return
        if await self._unsupported_op(target, "Session.credential", target.host.session.credential):
            return
        credential = await target.host.session.credential()
        assert isinstance(credential, Credential)
        assert credential.token
        assert credential.token not in repr(credential)

    async def test_repository_current_default_branch_display_name(self) -> None:
        """B-REP-3: current() is the target repository, default_branch() the seeded name, display_name() a str."""
        target = await self.make_target()
        if not self._declares(target, Capability.REPOSITORY):
            return
        target.seeder.set_default_branch("trunk")
        current = await target.host.repository.current()
        assert current == target.repository
        assert await target.host.repository.default_branch(current) == "trunk"
        if await self._unsupported_op(
            target, "Repository.display_name", lambda: target.host.repository.display_name(current)
        ):
            return
        name = await target.host.repository.display_name(current)
        assert isinstance(name, str)
        assert name

    # -- change requests -----------------------------------------------------------------------------------------

    async def test_get_returns_seeded_change_request(self) -> None:
        """B-CR-1: get returns the seeded number, state and head revision. A missing number raises CodeHostError."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo = target.repository
        merged = target.seeder.add_change_request(
            repo, head_reference="feature/a", head_revision="abc123", state=ChangeRequestState.MERGED
        )
        open_one = target.seeder.add_change_request(repo, head_reference="feature/b", head_revision="def456")
        for seeded in (merged, open_one):
            got = await target.host.change_requests.get(repo, seeded.number)
            assert got.number == seeded.number
            assert got.state is seeded.state
            assert got.head_revision == seeded.head_revision
            assert await target.host.change_requests.state(repo, seeded.number) is seeded.state
        for call in (
            lambda: target.host.change_requests.get(repo, open_one.number + 100),
            lambda: target.host.change_requests.state(repo, open_one.number + 100),
        ):
            try:
                await call()
            except UnsupportedCapabilityError:
                raise AssertionError("a missing number must raise a plain CodeHostError") from None
            except CodeHostError:
                pass
            else:
                raise AssertionError("CodeHostError not raised")

    async def test_find_by_branch_honors_state(self) -> None:
        """B-CR-2: find_by_branch returns the change requests of the branch, by state (default open)."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo, seeder, crs = target.repository, target.seeder, target.host.change_requests
        closed = seeder.add_change_request(repo, head_reference="feature/a", state=ChangeRequestState.CLOSED)
        open_one = seeder.add_change_request(repo, head_reference="feature/a")
        seeder.add_change_request(repo, head_reference="feature/b")
        found = await crs.find_by_branch(repo, "feature/a")
        assert [c.number for c in found] == [open_one.number]
        assert found[0].head_reference == "feature/a"
        closed_found = await crs.find_by_branch(repo, "feature/a", state=ChangeRequestState.CLOSED)
        assert [c.number for c in closed_found] == [closed.number]
        # Any state: the most recent match comes first.
        any_state = await crs.find_by_branch(repo, "feature/a", state=None)
        assert any_state
        assert any_state[0].number == open_one.number
        assert await crs.find_by_branch(repo, "feature/none") == []
        assert await crs.find_by_branch(repo, "feature/none", state=None) == []

    async def test_create_draft_is_visible_and_unique(self) -> None:
        """B-CR-5: create_draft returns a draft that get and find_by_branch see. A second one for it raises."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo, crs = target.repository, target.host.change_requests
        created = await crs.create_draft(repo, head="feature/new", base="main", title="A title", body="A body")
        assert created.is_draft is True
        assert created.number > 0
        assert created.state is ChangeRequestState.OPEN
        assert (await crs.get(repo, created.number)).number == created.number
        assert [c.number for c in await crs.find_by_branch(repo, "feature/new")] == [created.number]
        try:
            await crs.create_draft(repo, head="feature/new", base="main", title="Again", body="")
        except UnsupportedCapabilityError:
            raise AssertionError("a duplicate must raise a plain CodeHostError") from None
        except CodeHostError:
            pass
        else:
            raise AssertionError("CodeHostError not raised")

    # -- work items ----------------------------------------------------------------------------------------------

    async def test_work_item_get(self) -> None:
        """B-WI-1: get returns the seeded work item. A missing number returns None, not an error."""
        target = await self.make_target()
        if not self._declares(target, Capability.WORK_ITEMS):
            return
        repo = target.repository
        seeded = target.seeder.add_work_item(repo, title="Fix it", body="Details", labels=["bug"], assignees=["ann"])
        if await self._unsupported_op(target, "WorkItems.get", lambda: target.host.work_items.get(repo, seeded.number)):
            return
        got = await target.host.work_items.get(repo, seeded.number)
        assert got is not None
        assert (got.number, got.title, got.body, got.labels) == (seeded.number, "Fix it", "Details", ("bug",))
        assert [u.login for u in got.assignees] == ["ann"]
        assert got.state is WorkItemState.OPEN
        assert await target.host.work_items.get(repo, seeded.number + 100) is None

    async def test_list_open_filters_and_orders(self) -> None:
        """B-WI-2: list_open returns open items in insertion order, by assignee and label. Closed ones are out."""
        target = await self.make_target()
        if not self._declares(target, Capability.WORK_ITEMS):
            return
        repo, seeder, items = target.repository, target.seeder, target.host.work_items
        first = seeder.add_work_item(repo, title="one", labels=["bug", "ready"], assignees=["ann"])
        seeder.add_work_item(repo, title="done", labels=["bug"], assignees=["ann"], state=WorkItemState.CLOSED)
        second = seeder.add_work_item(repo, title="two", labels=["ready"], assignees=["bob"])
        third = seeder.add_work_item(repo, title="three", labels=["bug", "ready"], assignees=["ann", "bob"])
        everything = await items.list_open(repo)
        assert [w.number for w in everything] == [first.number, second.number, third.number]
        assert [w.title for w in everything] == ["one", "two", "three"]
        assert everything[0].labels == ("bug", "ready")
        assert [u.login for u in everything[2].assignees] == ["ann", "bob"]
        by_assignee = await items.list_open(repo, assignee="ann")
        assert [w.number for w in by_assignee] == [first.number, third.number]
        by_label = await items.list_open(repo, label="ready")
        assert [w.number for w in by_label] == [first.number, second.number, third.number]
        both = await items.list_open(repo, assignee="bob", label="bug")
        assert [w.number for w in both] == [third.number]

    async def test_list_open_respects_declared_bound(self) -> None:
        """B-WI-2: the result holds at most the declared list bound."""
        target = await self.make_target()
        if not self._declares(target, Capability.WORK_ITEMS):
            return
        bounds = target.host.declaration.limits.list_bounds
        bound = bounds.per_operation.get("WorkItems.list_open", bounds.default)
        if bound is None:
            return
        for index in range(bound + 3):
            target.seeder.add_work_item(target.repository, title=f"item {index}")
        assert len(await target.host.work_items.list_open(target.repository)) == bound

    async def test_remove_label(self) -> None:
        """B-WI-3: remove_label removes the label. A label the item does not carry is a no-op."""
        target = await self.make_target()
        if not self._declares(target, Capability.WORK_ITEMS):
            return
        repo, items = target.repository, target.host.work_items
        first = target.seeder.add_work_item(repo, title="one", labels=["x", "z"])
        second = target.seeder.add_work_item(repo, title="two", labels=["y"])

        async def labels() -> dict[int, tuple[str, ...]]:
            return {w.number: w.labels for w in await items.list_open(repo)}

        await items.remove_label(repo, first.number, "x")
        assert await labels() == {first.number: ("z",), second.number: ("y",)}
        await items.remove_label(repo, first.number, "y")  # the repository has "y"; the item does not carry it
        assert await labels() == {first.number: ("z",), second.number: ("y",)}

    async def test_closing_change_requests_and_reverse_relation(self) -> None:
        """B-WI-4: closing_change_requests returns what was linked. linked_work_items is the reverse. None: empty."""
        target = await self.make_target()
        if not self._declares(target, Capability.WORK_ITEMS):
            return
        repo, seeder, items = target.repository, target.seeder, target.host.work_items
        linked = seeder.add_work_item(repo, title="linked")
        alone = seeder.add_work_item(repo, title="alone")
        open_cr = seeder.add_change_request(repo, head_reference="feature/a")
        merged_cr = seeder.add_change_request(repo, head_reference="feature/b", state=ChangeRequestState.MERGED)
        unlinked_cr = seeder.add_change_request(repo, head_reference="feature/c")
        seeder.link_closing(repo, work_item=linked.number, change_request=open_cr.number)
        seeder.link_closing(repo, work_item=linked.number, change_request=merged_cr.number)
        closing = await items.closing_change_requests(repo, linked.number)
        assert {c.number: c.state for c in closing} == {
            open_cr.number: ChangeRequestState.OPEN,
            merged_cr.number: ChangeRequestState.MERGED,
        }
        assert list(await items.closing_change_requests(repo, alone.number)) == []

        async def reverse(number: int) -> Any:
            return await items.linked_work_items(repo, number)

        if await self._unsupported_op(target, "WorkItems.linked_work_items", lambda: reverse(open_cr.number)):
            return
        assert [w.number for w in await reverse(open_cr.number)] == [linked.number]
        assert [w.number for w in await reverse(merged_cr.number)] == [linked.number]
        assert list(await reverse(unlinked_cr.number)) == []

    async def test_open_change_request_bodies(self) -> None:
        """B-WI-5: open_change_request_bodies returns the open change requests with their descriptions."""
        target = await self.make_target()
        if not self._declares(target, Capability.WORK_ITEMS):
            return
        repo, seeder = target.repository, target.seeder
        assert list(await target.host.work_items.open_change_request_bodies(repo)) == []
        seeder.add_change_request(repo, head_reference="feature/a", body="Closes #1")
        seeder.add_change_request(repo, head_reference="feature/b", body="old", state=ChangeRequestState.CLOSED)
        seeder.add_change_request(repo, head_reference="feature/c", body="second")
        found = await target.host.work_items.open_change_request_bodies(repo)
        assert sorted(c.body for c in found) == ["Closes #1", "second"]  # the order is the host's

    # -- Session, Repository and ChangeRequests: the methods added after the review flow ---------------------------

    async def test_current_user_and_agent_environment(self) -> None:
        """B-SES-2, B-SES-4: the login is the configured one. The agent environment is base first, credential wins."""
        target = await self.make_target()
        if not self._declares(target, Capability.SESSION):
            return
        session = target.host.session
        target.seeder.set_user("octo")
        assert await session.current_user() == "octo"
        names = list(await session.agent_environment_names())
        assert names == ["GITHUB_TOKEN", "GH_TOKEN"]
        token = (await session.credential()).token
        base = {"PATH": "/bin", "GH_TOKEN": "stale", "ZED": "z"}
        env = await session.agent_environment(base)
        assert list(env)[: len(base)] == list(base)  # base entries come first and keep their place
        assert list(env)[len(base) :] == ["GITHUB_TOKEN"]
        assert env["GITHUB_TOKEN"] == env["GH_TOKEN"] == token  # the credential wins over a base entry
        assert (env["PATH"], env["ZED"]) == ("/bin", "z")
        assert base == {"PATH": "/bin", "GH_TOKEN": "stale", "ZED": "z"}
        bare = await session.agent_environment(None)
        assert list(bare) == names
        assert set(bare.values()) == {token}

    async def test_resolve_identity(self) -> None:
        """B-REP-1: ``resolve`` returns an equal ref for one identity. An empty identity raises ConfigurationError."""
        target = await self.make_target()
        if not self._declares(target, Capability.REPOSITORY):
            return
        repository = target.host.repository
        first = await repository.resolve("example/other")
        assert first == await repository.resolve("example/other")
        assert isinstance(first, RepositoryRef)
        try:
            await repository.resolve("")
        except ConfigurationError:
            pass
        else:
            raise AssertionError("ConfigurationError not raised")

    async def test_get_returns_the_full_field_set(self) -> None:
        """B-CR-1: ``get`` returns title, body, draft flag, author, branches and requested reviewers."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo = target.repository
        seeded = target.seeder.add_change_request(
            repo,
            head_reference="feature/full",
            base_reference="dev",
            title="A title",
            body="A body",
            is_draft=True,
            author="ann",
            head_revision="abc",
            requested_reviewers=["bob"],
        )
        got = await target.host.change_requests.get(repo, seeded.number)
        assert (got.title, got.body, got.is_draft) == ("A title", "A body", True)
        assert (got.head_reference, got.base_reference, got.head_revision) == ("feature/full", "dev", "abc")
        assert got.author is not None
        assert got.author.login == "ann"
        assert [u.login for u in got.requested_reviewers] == ["bob"]
        assert got.state is ChangeRequestState.OPEN

    async def test_list_filters(self) -> None:
        """B-CR-3: ``list`` filters by state, author and requested reviewer. No match is an empty sequence."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo, seeder, crs = target.repository, target.seeder, target.host.change_requests
        assert list(await crs.list(repo)) == []
        first = seeder.add_change_request(repo, head_reference="feature/a", author="ann", requested_reviewers=["bob"])
        second = seeder.add_change_request(repo, head_reference="feature/b", author="cat")
        closed = seeder.add_change_request(
            repo, head_reference="feature/c", author="ann", state=ChangeRequestState.CLOSED
        )

        async def numbers(**kwargs: Any) -> set[int]:
            return {c.number for c in await crs.list(repo, **kwargs)}

        assert await numbers() == {first.number, second.number}  # the order is the host's
        assert await numbers(author="ann") == {first.number}
        assert await numbers(requested_reviewer="bob") == {first.number}
        assert await numbers(state=ChangeRequestState.CLOSED) == {closed.number}
        assert await numbers(author="nobody") == set()
        assert await numbers(requested_reviewer="nobody") == set()
        listed = {c.number: c for c in await crs.list(repo)}
        assert listed[first.number].head_reference == "feature/a"
        assert listed[first.number].state is ChangeRequestState.OPEN

    async def test_find_by_commit_commits_and_reviews(self) -> None:
        """B-CR-4: ``find_by_commit``, ``commits`` (oldest first) and ``reviews`` return what was seeded."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo, seeder, crs = target.repository, target.seeder, target.host.change_requests
        one = seeder.add_change_request(repo, head_reference="feature/a", head_revision="r1")
        other = seeder.add_change_request(repo, head_reference="feature/b", head_revision="x1")
        seeder.add_commit(repo, one.number, revision="r0", message="first", authors=["ann"])
        seeder.add_commit(repo, one.number, revision="r1", message="second", authors=["bob"])
        assert [c.number for c in await crs.find_by_commit(repo, "r0")] == [one.number]
        assert [c.number for c in await crs.find_by_commit(repo, "r1")] == [one.number]
        assert list(await crs.find_by_commit(repo, "unknown")) == []
        commits = await crs.commits(repo, one.number)
        assert [(c.revision, c.message, [u.login for u in c.authors]) for c in commits] == [
            ("r0", "first", ["ann"]),
            ("r1", "second", ["bob"]),
        ]
        assert list(await crs.commits(repo, other.number)) == []
        assert list(await crs.reviews(repo, other.number)) == []
        seeder.add_review(repo, one.number, author="bob", state=ReviewState.APPROVED, revision="r1", body="ok")
        seeder.add_review(repo, one.number, author="cat", state=ReviewState.CHANGES_REQUESTED, body="no")
        reviews = await crs.reviews(repo, one.number)
        assert [(r.author.login if r.author else None, r.state, r.body) for r in reviews] == [
            ("bob", ReviewState.APPROVED, "ok"),
            ("cat", ReviewState.CHANGES_REQUESTED, "no"),
        ]
        assert reviews[0].revision == "r1"

    async def test_squash_merge_pins_the_head_revision(self) -> None:
        """B-CR-6: a merge at the current head makes the state merged. A head mismatch raises and changes nothing."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo, crs = target.repository, target.host.change_requests
        stale = target.seeder.add_change_request(repo, head_reference="feature/a", head_revision="new-head")
        fresh = target.seeder.add_change_request(repo, head_reference="feature/b", head_revision="head-b")
        try:
            await crs.squash_merge(repo, stale.number, expected_head="old-head")
        except UnsupportedCapabilityError:
            raise AssertionError("a head mismatch must raise a plain CodeHostError") from None
        except CodeHostError:
            pass
        else:
            raise AssertionError("CodeHostError not raised")
        assert (await crs.get(repo, stale.number)).state is ChangeRequestState.OPEN
        await crs.squash_merge(repo, fresh.number, expected_head="head-b")
        assert (await crs.get(repo, fresh.number)).state is ChangeRequestState.MERGED
        assert (await crs.get(repo, stale.number)).state is ChangeRequestState.OPEN

    async def test_request_reviewer_and_approve(self) -> None:
        """B-CR-7: the reviewer is requested. An approval at ``head_revision`` is an approved review at it."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo, crs = target.repository, target.host.change_requests
        cr = target.seeder.add_change_request(repo, head_reference="feature/a", head_revision="head-a")
        await crs.request_reviewer(repo, cr.number, "bob")
        assert [u.login for u in (await crs.get(repo, cr.number)).requested_reviewers] == ["bob"]
        assert list(await crs.reviews(repo, cr.number)) == []
        await crs.approve(repo, cr.number, head_revision="head-a")
        (review,) = await crs.reviews(repo, cr.number)
        assert review.state is ReviewState.APPROVED
        assert review.revision == "head-a"

    async def test_viewed_files(self) -> None:
        """B-CR-8: a viewed file is reflected in ``viewed_files``. Marking twice leaves it viewed."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHANGE_REQUESTS):
            return
        repo, crs = target.repository, target.host.change_requests
        cr = target.seeder.add_change_request(repo, head_reference="feature/a")
        assert dict(await crs.viewed_files(repo, cr.number)) == {}
        await crs.mark_viewed(repo, cr.number, "src/a.py")
        await crs.mark_viewed(repo, cr.number, "src/b.py")
        await crs.mark_viewed(repo, cr.number, "src/a.py")
        assert dict(await crs.viewed_files(repo, cr.number)) == {"src/a.py": True, "src/b.py": True}

    # -- discussion ----------------------------------------------------------------------------------------------

    async def test_comments_and_post_comment(self) -> None:
        """B-DIS-1: ``post_comment`` returns the comment and ``comments`` lists it, in posting order."""
        target = await self.make_target()
        if not self._declares(target, Capability.DISCUSSION):
            return
        repo, seeder, discussion = target.repository, target.seeder, target.host.discussion
        cr = seeder.add_change_request(repo, head_reference="feature/a")
        other = seeder.add_change_request(repo, head_reference="feature/b")
        assert list(await discussion.comments(repo, cr.number)) == []
        seeder.add_comment(repo, cr.number, author="bob", body="first")
        seeder.add_comment(repo, cr.number, author="cat", body="second")
        before = await discussion.comments(repo, cr.number)
        assert [(c.author.login if c.author else None, c.body) for c in before] == [("bob", "first"), ("cat", "second")]
        posted = await discussion.post_comment(repo, cr.number, "third")
        assert posted.body == "third"
        assert [c.body for c in await discussion.comments(repo, cr.number)] == ["first", "second", "third"]
        assert list(await discussion.comments(repo, other.number)) == []

    async def test_review_threads_and_reply(self) -> None:
        """B-DIS-2, B-DIS-3: threads keep ``is_resolved``, anchor and comments. A reply joins its thread."""
        target = await self.make_target()
        if not self._declares(target, Capability.DISCUSSION):
            return
        repo, seeder, discussion = target.repository, target.seeder, target.host.discussion
        cr = seeder.add_change_request(repo, head_reference="feature/a")
        assert list(await discussion.review_threads(repo, cr.number)) == []
        anchor = Anchor("src/a.py", 3, "RIGHT")
        open_thread = seeder.add_thread(repo, cr.number, anchor=anchor, comments=[_comment("1001", "bob", "why?")])
        seeder.add_thread(
            repo,
            cr.number,
            anchor=Anchor("src/b.py", 9, "RIGHT"),
            comments=[_comment("1002", "cat", "ok")],
            is_resolved=True,
        )
        threads = await discussion.review_threads(repo, cr.number)
        assert [(t.is_resolved, t.anchor) for t in threads] == [(False, anchor), (True, Anchor("src/b.py", 9, "RIGHT"))]
        assert [[c.body for c in t.comments] for t in threads] == [["why?"], ["ok"]]
        reply = await discussion.reply(repo, cr.number, open_thread.id, "because")
        assert reply.body == "because"
        after = await discussion.review_threads(repo, cr.number)
        assert [c.body for c in after[0].comments] == ["why?", "because"]
        assert [c.body for c in after[1].comments] == ["ok"]
        try:
            await discussion.reply(repo, cr.number, "no-such-thread", "x")
        except UnsupportedCapabilityError:
            raise AssertionError("an unknown thread must raise a plain CodeHostError") from None
        except CodeHostError:
            pass
        else:
            raise AssertionError("CodeHostError not raised")

    # -- checks --------------------------------------------------------------------------------------------------

    async def test_list_checks_and_required_only(self) -> None:
        """B-CHK-1: ``list_checks`` returns the seeded checks. ``required_only`` keeps the required ones. None is empty."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHECKS):
            return
        repo, seeder, checks = target.repository, target.seeder, target.host.checks
        cr = seeder.add_change_request(repo, head_reference="feature/a")
        assert list(await checks.list_checks(repo, cr.number)) == []
        assert list(await checks.list_checks(repo, cr.number, required_only=True)) == []
        seeder.add_check(repo, cr.number, name="build", status=CheckStatus.FAIL, required=True, job_id="900")
        seeder.add_check(repo, cr.number, name="lint", status=CheckStatus.PASS)
        seeder.add_check(repo, cr.number, name="deploy", status=CheckStatus.PENDING)
        found = await checks.list_checks(repo, cr.number)
        assert [(c.name, c.status) for c in found] == [
            ("build", CheckStatus.FAIL),
            ("lint", CheckStatus.PASS),
            ("deploy", CheckStatus.PENDING),
        ]
        assert [c.job_id for c in found] == ["900", None, None]
        assert all(c.details_url for c in found)
        assert found[0].completed_at is not None
        assert found[2].completed_at is None
        required = await checks.list_checks(repo, cr.number, required_only=True)
        assert [c.name for c in required] == ["build"]

    async def test_list_workflows(self) -> None:
        """B-CHK-2: ``list_workflows`` returns the seeded workflows, and an empty sequence when there are none."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHECKS):
            return
        repo, checks = target.repository, target.host.checks
        assert list(await checks.list_workflows(repo)) == []
        target.seeder.add_workflow(repo, name="ci")
        target.seeder.add_workflow(repo, name="release")
        assert [w.name for w in await checks.list_workflows(repo)] == ["ci", "release"]

    async def test_failed_log_and_get_job(self) -> None:
        """B-CHK-3: ``failed_log`` returns the log, ``get_job`` the failed step. An unknown job raises CodeHostError."""
        target = await self.make_target()
        if not self._declares(target, Capability.CHECKS):
            return
        repo, seeder, checks = target.repository, target.seeder, target.host.checks
        cr = seeder.add_change_request(repo, head_reference="feature/a")
        seeder.add_job(repo, job_id="900", failed_step="Run tests", log="boom in step")
        seeder.add_job(repo, job_id="901")
        seeder.add_check(repo, cr.number, name="build", status=CheckStatus.FAIL, job_id="900")
        (check,) = await checks.list_checks(repo, cr.number)
        assert "boom in step" in await checks.failed_log(repo, check)
        job = await checks.get_job(repo, "900")
        assert (job.id, job.failed_step) == ("900", "Run tests")
        assert (await checks.get_job(repo, "901")).failed_step is None
        for call in (
            lambda: checks.get_job(repo, "404"),
            lambda: checks.failed_log(repo, Check("none", CheckStatus.FAIL, None, None, None, None, None)),
        ):
            try:
                await call()
            except UnsupportedCapabilityError:
                raise AssertionError("an unknown job must raise a plain CodeHostError") from None
            except CodeHostError:
                pass
            else:
                raise AssertionError("CodeHostError not raised")

    # -- empty, failed, timed out: one table for every method added after the review flow -------------------------

    def _calls(self, target: ConformanceTarget) -> dict[str, tuple[bool, Callable[[], Awaitable[Any]]]]:
        """Seed one change request with everything on it. Return ``operation -> (is a write, call)``.

        Only operations of declared capabilities that the target does not list as unsupported are returned.
        """
        repo, seeder, host = target.repository, target.seeder, target.host
        cr = seeder.add_change_request(repo, head_reference="feature/ops", head_revision="rev-ops", author="ann")
        n = cr.number
        seeder.add_commit(repo, n, revision="rev-ops", message="a commit", authors=["ann"])
        seeder.add_review(repo, n, author="bob", state=ReviewState.COMMENTED, revision="rev-ops")
        seeder.add_comment(repo, n, author="bob", body="a comment")
        thread = seeder.add_thread(
            repo, n, anchor=Anchor("src/a.py", 1, "RIGHT"), comments=[_comment("2001", "bob", "t")]
        )
        seeder.add_job(repo, job_id="900", failed_step="Run tests", log="log text")
        seeder.add_check(repo, n, name="build", status=CheckStatus.FAIL, job_id="900")
        seeder.add_workflow(repo, name="ci")
        item = seeder.add_work_item(repo, title="an item")
        seeder.link_closing(repo, work_item=item.number, change_request=n)
        check = Check("build", CheckStatus.FAIL, "https://example.test/runs/1/job/900", None, None, None, "900")
        crs, discussion, checks, items = host.change_requests, host.discussion, host.checks, host.work_items
        table: dict[str, tuple[bool, Callable[[], Awaitable[Any]]]] = {
            "Session.current_user": (False, lambda: host.session.current_user()),
            "ChangeRequests.list": (False, lambda: crs.list(repo)),
            "ChangeRequests.find_by_commit": (False, lambda: crs.find_by_commit(repo, "rev-ops")),
            "ChangeRequests.commits": (False, lambda: crs.commits(repo, n)),
            "ChangeRequests.reviews": (False, lambda: crs.reviews(repo, n)),
            "ChangeRequests.viewed_files": (False, lambda: crs.viewed_files(repo, n)),
            "ChangeRequests.squash_merge": (True, lambda: crs.squash_merge(repo, n, expected_head="rev-ops")),
            "ChangeRequests.request_reviewer": (True, lambda: crs.request_reviewer(repo, n, "bob")),
            "ChangeRequests.approve": (True, lambda: crs.approve(repo, n, head_revision="rev-ops")),
            "ChangeRequests.mark_viewed": (True, lambda: crs.mark_viewed(repo, n, "src/a.py")),
            "Discussion.comments": (False, lambda: discussion.comments(repo, n)),
            "Discussion.review_threads": (False, lambda: discussion.review_threads(repo, n)),
            "Discussion.post_comment": (True, lambda: discussion.post_comment(repo, n, "new")),
            "Discussion.reply": (True, lambda: discussion.reply(repo, n, thread.id, "new")),
            "Checks.list_checks": (False, lambda: checks.list_checks(repo, n)),
            "Checks.list_workflows": (False, lambda: checks.list_workflows(repo)),
            "Checks.failed_log": (False, lambda: checks.failed_log(repo, check)),
            "Checks.get_job": (False, lambda: checks.get_job(repo, "900")),
            "WorkItems.get": (False, lambda: items.get(repo, item.number)),
            "WorkItems.linked_work_items": (False, lambda: items.linked_work_items(repo, n)),
        }
        return {
            operation: entry
            for operation, entry in table.items()
            if self._declares(target, Capability(operation.split(".")[0])) and operation not in target.unsupported
        }

    async def test_empty_result_is_not_a_failure(self) -> None:
        """Nothing found is an empty sequence (``None`` for ``WorkItems.get``, an empty mapping for viewed files)."""
        target = await self.make_target()
        repo, seeder, host = target.repository, target.seeder, target.host
        cr = seeder.add_change_request(repo, head_reference="feature/empty")
        n = cr.number
        if self._declares(target, Capability.CHANGE_REQUESTS):
            crs = host.change_requests
            assert list(await crs.find_by_commit(repo, "unknown")) == []
            assert list(await crs.commits(repo, n)) == []
            assert list(await crs.reviews(repo, n)) == []
            assert dict(await crs.viewed_files(repo, n)) == {}
            assert list(await crs.list(repo, author="nobody")) == []
        if self._declares(target, Capability.DISCUSSION):
            assert list(await host.discussion.comments(repo, n)) == []
            assert list(await host.discussion.review_threads(repo, n)) == []
        if self._declares(target, Capability.CHECKS):
            assert list(await host.checks.list_checks(repo, n)) == []
            assert list(await host.checks.list_workflows(repo)) == []
        if self._declares(target, Capability.WORK_ITEMS):
            assert await host.work_items.get(repo, 12345) is None
            assert list(await host.work_items.linked_work_items(repo, n)) == []

    async def test_failed_result_raises_code_host_error(self) -> None:
        """A request that failed raises CodeHostError. It is never returned as an empty result."""
        probe = await self.make_target()
        for operation in self._calls(probe):
            target = await self.make_target()
            _, call = self._calls(target)[operation]
            target.seeder.fail_next(operation, CodeHostError("the request failed"))
            try:
                await call()
            except UnsupportedCapabilityError:
                raise AssertionError(f"{operation}: a failure must not read as unsupported") from None
            except CodeHostTimeoutError:
                raise AssertionError(f"{operation}: a failure must not read as a timeout") from None
            except CodeHostError:
                pass
            else:
                raise AssertionError(f"{operation} returned a result although the request failed")

    async def test_timeout_flag_follows_read_or_write(self) -> None:
        """B-ERR-2: a read raises CodeHostTimeoutError with ``write=False``, a write with ``write=True``."""
        probe = await self.make_target()
        for operation, (is_write, _) in self._calls(probe).items():
            target = await self.make_target()
            _, call = self._calls(target)[operation]
            target.seeder.fail_next(operation, CodeHostTimeoutError("timed out", write=is_write))
            try:
                await call()
            except CodeHostTimeoutError as exc:
                assert exc.write is is_write, operation
            else:
                raise AssertionError(f"{operation} did not time out")

    async def test_unsupported_operations_raise(self) -> None:
        """B-ERR-4: an operation the target lists as unsupported raises UnsupportedCapabilityError."""
        target = await self.make_target()
        if not target.unsupported:
            return
        full_target = ConformanceTarget(
            host=target.host, seeder=target.seeder, repository=target.repository, secret=target.secret
        )
        everything = self._calls(full_target)
        for operation in target.unsupported & everything.keys():
            _, call = everything[operation]
            assert await self._unsupported_op(target, operation, call)

    # -- list bounds -----------------------------------------------------------------------------------------------

    @staticmethod
    def _bound(target: ConformanceTarget, operation: str) -> int | None:
        bounds = target.host.declaration.limits.list_bounds
        return bounds.per_operation.get(operation, bounds.default)

    async def test_unbounded_reads_return_every_item(self) -> None:
        """B-CR-4, B-DIS-2, DEC-31: a read declared unbounded returns every item over a page boundary."""
        count = 201  # three pages of 100
        seeders: dict[str, Callable[[ConformanceTarget, int, int], Any]] = {
            "ChangeRequests.commits": lambda t, n, i: t.seeder.add_commit(t.repository, n, revision=f"r{i}"),
            "ChangeRequests.reviews": lambda t, n, i: t.seeder.add_review(
                t.repository, n, author="bob", state=ReviewState.COMMENTED, body=f"review {i}"
            ),
            "Discussion.comments": lambda t, n, i: t.seeder.add_comment(t.repository, n, author="bob", body=f"c {i}"),
            "Discussion.review_threads": lambda t, n, i: t.seeder.add_thread(
                t.repository,
                n,
                anchor=Anchor("src/a.py", i + 1, "RIGHT"),
                comments=[_comment(str(5000 + i), "bob", "c")],
            ),
        }
        for operation, seed in seeders.items():
            target = await self.make_target()
            if not self._declares(target, Capability(operation.split(".")[0])) or self._bound(target, operation):
                continue
            cr = target.seeder.add_change_request(target.repository, head_reference="feature/many")
            for i in range(count):
                seed(target, cr.number, i)
            _, call = self._calls_for(target, cr.number)[operation]
            assert len(await call()) == count, operation
        # Viewed files are seeded through the host itself.
        target = await self.make_target()
        if (
            self._declares(target, Capability.CHANGE_REQUESTS)
            and self._bound(target, "ChangeRequests.viewed_files") is None
        ):
            repo, crs = target.repository, target.host.change_requests
            cr = target.seeder.add_change_request(repo, head_reference="feature/many")
            for i in range(count):
                await crs.mark_viewed(repo, cr.number, f"src/file{i}.py")
            assert len(await crs.viewed_files(repo, cr.number)) == count

    def _calls_for(
        self, target: ConformanceTarget, number: int
    ) -> dict[str, tuple[bool, Callable[[], Awaitable[Any]]]]:
        repo, host = target.repository, target.host
        return {
            "ChangeRequests.commits": (False, lambda: host.change_requests.commits(repo, number)),
            "ChangeRequests.reviews": (False, lambda: host.change_requests.reviews(repo, number)),
            "Discussion.comments": (False, lambda: host.discussion.comments(repo, number)),
            "Discussion.review_threads": (False, lambda: host.discussion.review_threads(repo, number)),
        }

    async def test_bounded_reads_never_truncate(self) -> None:
        """DEC-31: a read over its declared bound raises CodeHostError or returns every item. It never cuts short."""
        target = await self.make_target()
        repo = target.repository
        bound = self._bound(target, "ChangeRequests.find_by_commit")
        if self._declares(target, Capability.CHANGE_REQUESTS):
            count = (bound or 100) + 1
            for i in range(count):
                cr = target.seeder.add_change_request(repo, head_reference=f"feature/{i}", head_revision=f"head-{i}")
                target.seeder.add_commit(repo, cr.number, revision="shared")
            try:
                found = await target.host.change_requests.find_by_commit(repo, "shared")
            except CodeHostError:
                pass
            else:
                assert len(found) == count
        target = await self.make_target()
        bound = self._bound(target, "Checks.list_workflows")
        if self._declares(target, Capability.CHECKS):
            count = (bound or 100) + 1
            for i in range(count):
                target.seeder.add_workflow(target.repository, name=f"workflow {i}")
            try:
                workflows = await target.host.checks.list_workflows(target.repository)
            except CodeHostError:
                pass
            else:
                assert len(workflows) == count

    # -- secrets -----------------------------------------------------------------------------------------------

    async def test_secret_never_leaks_from_any_error_type(self) -> None:
        """B-ERR-5: a credential `<token>` is in no message, repr or cause of any error type of any method."""
        assert (await self.make_target()).secret == SECRET, (
            "make_target must build the host with SECRET as its credential"
        )
        handler = _LogCapture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        seen: list[str] = []
        try:
            probe = await self.make_target()
            for operation, (is_write, _) in self._calls(probe).items():
                capability = operation.split(".")[0]
                errors: list[CodeHostError] = [
                    CodeHostError("request failed"),
                    CodeHostTimeoutError("timed out", write=is_write),
                    AuthenticationError("not authenticated"),
                    UnsupportedCapabilityError("not supported", capability=capability),
                    ConfigurationError("bad configuration"),
                    NoChangeRequestError("no change request"),
                ]
                for error in errors:
                    target = await self.make_target()
                    _, call = self._calls(target)[operation]
                    target.seeder.fail_next(operation, error)
                    try:
                        result = await call()
                    except CodeHostError as exc:  # any other exception type propagates and fails the test
                        for item in _chain(exc):
                            seen.extend([str(item), repr(item), repr(item.args)])
                    else:
                        seen.append(repr(result))
                    seen.extend([repr(target.host), repr(target.host.declaration)])
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        seen.extend(handler.text)
        assert seen
        leaks = [s for s in seen if SECRET in s]
        assert not leaks, leaks

    # -- errors --------------------------------------------------------------------------------------------------

    async def test_undeclared_capability_substitution(self) -> None:
        """B-ERR-4: every method of an undeclared capability raises UnsupportedCapabilityError(capability=name)."""
        target = await self.make_target()
        host = target.host
        objects = {
            Capability.SESSION: host.session,
            Capability.REPOSITORY: host.repository,
            Capability.CHANGE_REQUESTS: host.change_requests,
            Capability.DISCUSSION: host.discussion,
            Capability.CHECKS: host.checks,
            Capability.WORK_ITEMS: host.work_items,
        }
        undeclared = [c for c in Capability if c not in host.declaration.capabilities]
        for capability in undeclared:
            protocol = getattr(interfaces, capability.value)
            for name in _public_methods(protocol):
                method = getattr(objects[capability], name)
                args, kwargs = _dummy_args(getattr(protocol, name))
                try:
                    await method(*args, **kwargs)
                except UnsupportedCapabilityError as exc:
                    assert exc.capability == capability.value, (capability, name)
                else:
                    raise AssertionError(f"{capability.value}.{name} did not raise")

    async def test_errors_are_code_host_errors(self) -> None:  # NOSONAR python:S7503 - suite tests are all coroutines
        """B-ERR-6: every error the review flow raises is a CodeHostError."""
        for error in (
            NoChangeRequestError("a"),
            CodeHostTimeoutError("b", write=False),
            AuthenticationError("c"),
            UnsupportedCapabilityError("d", capability="Checks"),
            ConfigurationError("e"),
        ):
            assert isinstance(error, CodeHostError)

    # -- secrets -------------------------------------------------------------------------------------------------

    async def test_secret_sentinel_never_leaks(self) -> None:
        """B-SEC-1: the token `<token>` is in no exception message, __cause__ chain, log record or repr."""
        assert (await self.make_target()).secret == SECRET, (
            "make_target must build the host with SECRET as its credential"
        )
        handler = _LogCapture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        seen: list[str] = []
        try:
            scenarios: list[Callable[[ConformanceTarget], None]] = [
                lambda t: t.seeder.set_current_change_request(None),
                lambda t: t.seeder.reject_inline_reviews("rejected"),
                lambda t: t.seeder.reject_all_posts("all rejected"),
                lambda t: t.seeder.fail_posts_with(CodeHostTimeoutError("timed out", write=True)),
                lambda t: t.seeder.fail_posts_with(AuthenticationError("not authenticated")),
                lambda t: t.seeder.set_available(False),
            ]
            for arrange in scenarios:
                target = await self.make_target()
                arrange(target)
                seen.extend([repr(target.host), repr(target.host.declaration)])
                for call in (
                    target.host.change_requests.for_current_branch(target.repository),
                    self._post(target),
                ):
                    try:
                        result = await call
                    except Exception as exc:
                        for item in _chain(exc):
                            seen.extend([str(item), repr(item), repr(item.args)])
                    else:
                        seen.append(repr(result))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        seen.extend(handler.text)
        assert seen
        leaks = [s for s in seen if SECRET in s]
        assert not leaks, leaks
