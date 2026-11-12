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
from collections.abc import Callable, Mapping, Sequence
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
    Credential,
    InlineComment,
    RepositoryRef,
    ReviewPostResult,
    User,
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


def _unsupported(capability: Capability, operation: str) -> UnsupportedCapabilityError:
    msg = f"{capability.value}.{operation} is not supported by the in-memory code host"
    return UnsupportedCapabilityError(msg, capability=capability.value)


# Capabilities the fake implements. Checks is not implemented yet: the fake never declares it.
_IMPLEMENTED = frozenset(Capability) - {Capability.CHECKS}
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


class InMemoryCodeHost:
    """A host that keeps everything in memory. It is its own seeder.

    ``capabilities`` selects the declared subset (default: every capability the fake implements, which is all but
    Checks). An undeclared capability raises ``UnsupportedCapabilityError`` from each method. Limits and anchors are
    the reference host values unless ``declaration`` is given. No network, no subprocess, no clock: timestamps come
    from a counter that adds one second per created object.

    Seeding: ``add_work_item``, ``add_change_request``, ``link_closing``, ``set_default_branch``, ``set_repository``,
    ``set_user``, and the review-flow methods. Failure injection: ``fail_next``, ``fail_always``, ``clear_failures``.
    Inspection: ``calls``, ``writes``, ``work_items``, ``change_requests``, ``posts``.
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
        declared = _IMPLEMENTED if capabilities is None else frozenset(capabilities)
        if declared - _IMPLEMENTED:
            msg = "the in-memory code host does not implement: " + ", ".join(
                sorted(c.value for c in declared - _IMPLEMENTED)
            )
            raise ConfigurationError(msg)
        self._repository = RepositoryRef(repository)
        self._user = user
        self._token = token
        self._default_branch = "main"
        self._available = True
        self._change_request: tuple[int, str] | None = None
        self._failure = _Failure()
        self._posts: list[PostedItem] = []
        self._attempts = 0
        self._work_items: dict[RepositoryRef, list[WorkItem]] = {}
        self._change_requests: dict[RepositoryRef, list[ChangeRequest]] = {}
        self._links: dict[RepositoryRef, list[tuple[int, int]]] = {}
        self._labels: dict[RepositoryRef, set[str]] = {}
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
        self._change_request = None if number is None else (number, base)

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
            call = self._record(operation, args, kwargs, type(exc).__name__)
            if self._version != before:
                self._writes.append(call)
            raise
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
            checks=facet(Capability.CHECKS, None),
            work_items=facet(Capability.WORK_ITEMS, _FakeWorkItems(self)),
            declaration=self.declaration,
        )

    def target(self) -> ConformanceTarget:
        return ConformanceTarget(host=self.code_host(), seeder=self, repository=self._repository, secret=self._token)

    def _bound(self, operation: str) -> int | None:
        bounds = self.declaration.limits.list_bounds
        return bounds.per_operation.get(operation, bounds.default)


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
        raise _unsupported(Capability.SESSION, "agent_environment_names")

    async def agent_environment(  # NOSONAR python:S7503 - implements an async protocol
        self, base: Mapping[str, str] | None = None
    ) -> Mapping[str, str]:
        raise _unsupported(Capability.SESSION, "agent_environment")


class _FakeRepository(_Facet):
    async def current(self) -> RepositoryRef:  # NOSONAR python:S7503 - implements an async protocol
        return self._do("Repository.current", lambda: self._host._repository)

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
            current = self._host._change_request
            if current is None:
                raise NoChangeRequestError(NO_CHANGE_REQUEST_TEXT)
            number, base = current
            return ChangeRequest(
                repository=repo,
                number=number,
                title="",
                body="",
                url="",
                state=ChangeRequestState.OPEN,
                is_draft=False,
                author=None,
                head_reference="",
                head_revision="",
                base_reference=base,
                is_cross_repository=False,
                created_at=_EPOCH,
            )

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

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _unsupported_op(*_args: object, **_kwargs: object) -> Any:  # NOSONAR python:S7503 - callers await it
            raise _unsupported(Capability.CHANGE_REQUESTS, name)

        return _unsupported_op


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

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _unsupported_op(*_args: object, **_kwargs: object) -> Any:  # NOSONAR python:S7503 - callers await it
            raise _unsupported(Capability.DISCUSSION, name)

        return _unsupported_op


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
