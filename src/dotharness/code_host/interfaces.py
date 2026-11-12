"""Capability interfaces of the code host. Every method is async."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from dotharness.code_host.declarations import HostDeclaration
from dotharness.code_host.models import (
    ChangeRequest,
    ChangeRequestState,
    Check,
    Comment,
    CommitInfo,
    Credential,
    InlineComment,
    Job,
    RepositoryRef,
    Review,
    ReviewPostResult,
    ReviewThread,
    Workflow,
    WorkItem,
)


class Session(Protocol):
    async def available(self) -> bool:
        """True when the host tool or service can be used. Never raises."""
        ...

    async def credential(self) -> Credential:
        """Resolve the token. Raises AuthenticationError, CodeHostTimeoutError."""
        ...

    async def current_user(self) -> str:
        """Login of the acting user. Raises AuthenticationError, CodeHostTimeoutError."""
        ...

    async def agent_environment_names(self) -> Sequence[str]:
        """Names of the variables a child agent process sees for this host, as data."""
        ...

    async def agent_environment(self, base: Mapping[str, str] | None = None) -> Mapping[str, str]:
        """base plus the credential variables, base entries first so credential variables win."""
        ...


class Repository(Protocol):
    async def current(self) -> RepositoryRef:
        """cwd mode: the repository of the clone. Raises ConfigurationError when it has no usable identity."""
        ...

    async def resolve(self, identity: str) -> RepositoryRef:
        """explicit mode: host-defined identity string to a ref. Raises ConfigurationError."""
        ...

    async def default_branch(self, repo: RepositoryRef) -> str:
        """Name of the default branch. Raises CodeHostError."""
        ...

    async def display_name(self, repo: RepositoryRef) -> str:
        """Human-readable identity for logs and messages only. Consumers must not parse it."""
        ...


class ChangeRequests(Protocol):
    async def get(self, repo: RepositoryRef, number: int) -> ChangeRequest:
        """Raises CodeHostError when the change request does not exist."""
        ...

    async def state(self, repo: RepositoryRef, number: int) -> ChangeRequestState:
        """Lifecycle state only (one narrow read). Raises CodeHostError when it does not exist."""
        ...

    async def find_by_branch(
        self, repo: RepositoryRef, branch: str, *, state: ChangeRequestState = ChangeRequestState.OPEN
    ) -> Sequence[ChangeRequest]:
        """Change requests whose head is `branch`. Empty when none."""
        ...

    async def for_current_branch(self, repo: RepositoryRef) -> ChangeRequest:
        """The open change request of the checked-out branch.

        Raises NoChangeRequestError when there is none and CodeHostError otherwise.
        """
        ...

    async def list(
        self,
        repo: RepositoryRef,
        *,
        state: ChangeRequestState = ChangeRequestState.OPEN,
        author: str | None = None,
        assignee: str | None = None,
        requested_reviewer: str | None = None,
    ) -> Sequence[ChangeRequest]:
        """Filters are logins; the consumer resolves "me" with Session.current_user()."""
        ...

    async def find_by_commit(self, repo: RepositoryRef, revision: str) -> Sequence[ChangeRequest]:
        """Change requests that contain the commit."""
        ...

    async def commits(self, repo: RepositoryRef, number: int) -> Sequence[CommitInfo]:
        """Commits with their dates, oldest first."""
        ...

    async def reviews(self, repo: RepositoryRef, number: int) -> Sequence[Review]:
        """All reviews (author, verdict, commit reviewed)."""
        ...

    async def create_draft(self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str) -> ChangeRequest:
        """Raises CodeHostError (including a conflicting existing change request)."""
        ...

    async def squash_merge(self, repo: RepositoryRef, number: int, *, expected_head: str) -> None:
        """Squash-merge only if the head revision equals `expected_head`. Raises CodeHostError on mismatch."""
        ...

    async def request_reviewer(self, repo: RepositoryRef, number: int, login: str) -> None: ...

    async def approve(self, repo: RepositoryRef, number: int, *, head_revision: str) -> None:
        """Approve the change request at `head_revision`."""
        ...

    async def viewed_files(self, repo: RepositoryRef, number: int) -> Mapping[str, bool]:
        """Path to viewed flag for the acting user."""
        ...

    async def mark_viewed(self, repo: RepositoryRef, number: int, path: str) -> None: ...


class Discussion(Protocol):
    async def comments(self, repo: RepositoryRef, number: int) -> Sequence[Comment]:
        """Change-request-level comments."""
        ...

    async def review_threads(self, repo: RepositoryRef, number: int) -> Sequence[ReviewThread]:
        """Inline review comments grouped in threads, each with its resolved state."""
        ...

    async def post_comment(self, repo: RepositoryRef, number: int, body: str) -> Comment: ...

    async def post_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        body: str,
        inline_comments: Sequence[InlineComment],
        head_revision: str | None = None,
        fallback_body: str | None = None,
    ) -> ReviewPostResult:
        """Post a review. If the host rejects the review or its anchors, post a top-level comment instead.

        The caller renders the text. The host applies its declared size limits: it truncates `body` and
        `fallback_body` and drops inline comments over the cap.

        - No fallback: `body` and `inline_comments` are posted as given.
        - Fallback: `fallback_body` (or `body` when it is None) is posted as one top-level comment.
          `ReviewPostResult.fell_back` is true and `reason` carries the rejection text.

        The host never falls back after `CodeHostTimeoutError`, `AuthenticationError` or
        `UnsupportedCapabilityError`; those propagate. It falls back at most once. If the top-level post also
        fails, that error propagates (`CodeHostError`). Size limits and anchor rules are declared, not parameters.
        May raise OSError when the host client is unavailable.
        """
        ...

    async def reply(self, repo: RepositoryRef, number: int, thread_id: str, body: str) -> Comment:
        """Reply in a review thread; `thread_id` is the opaque id from ReviewThread."""
        ...


class Checks(Protocol):
    async def list_checks(self, repo: RepositoryRef, number: int, *, required_only: bool = False) -> Sequence[Check]:
        """Checks of a change request, optionally only those the host requires."""
        ...

    async def list_workflows(self, repo: RepositoryRef) -> Sequence[Workflow]:
        """Empty when the repository has none."""
        ...

    async def failed_log(self, repo: RepositoryRef, check: Check) -> str:
        """Log of the failing part of `check`. Raises CodeHostError."""
        ...

    async def get_job(self, repo: RepositoryRef, job_id: str) -> Job:
        """A job with its failing step name; `job_id` is the opaque id from Check."""
        ...


class WorkItems(Protocol):
    async def get(self, repo: RepositoryRef, number: int) -> WorkItem | None:
        """Title and body. None when confirmed absent; raises CodeHostError only for an inconclusive failure."""
        ...

    async def list_open(
        self, repo: RepositoryRef, *, assignee: str | None = None, label: str | None = None
    ) -> Sequence[WorkItem]: ...

    async def remove_label(self, repo: RepositoryRef, number: int, label: str) -> None: ...

    async def closing_change_requests(self, repo: RepositoryRef, number: int) -> Sequence[ChangeRequest]:
        """Change requests the host records as closing the work item."""
        ...

    async def open_change_request_bodies(self, repo: RepositoryRef) -> Sequence[ChangeRequest]:
        """Open change requests with their descriptions, for a consumer-side scan. Bounded by a declared limit."""
        ...

    async def linked_work_items(self, repo: RepositoryRef, number: int) -> Sequence[WorkItem]:
        """Work items the host records as closed by the change request."""
        ...


@dataclass(frozen=True)
class CodeHost:
    """The six capability objects plus the host's declaration.

    Undeclared capabilities are objects that raise UnsupportedCapabilityError(capability=<interface name>).
    """

    session: Session
    repository: Repository
    change_requests: ChangeRequests
    discussion: Discussion
    checks: Checks
    work_items: WorkItems
    declaration: HostDeclaration
