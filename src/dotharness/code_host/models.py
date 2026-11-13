"""Host-neutral value types accepted and returned by the capability interfaces."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from dotharness.code_host.declarations import Anchor


class ChangeRequestState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class MergeableState(Enum):
    MERGEABLE = "mergeable"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class ReviewDecision(Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REVIEW_REQUIRED = "review_required"


class ReviewState(Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    DISMISSED = "dismissed"
    PENDING = "pending"


class CheckStatus(Enum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    PENDING = "pending"
    SKIPPING = "skipping"
    CANCEL = "cancel"


class WorkItemState(Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class RepositoryRef:
    """Opaque, host-defined repository identity. Consumers pass it through, compare it and hash it."""

    value: str


@dataclass(frozen=True)
class Credential:
    """Resolved host credential. The token is never shown by repr; examples write it as <token>."""

    token: str = field(repr=False)


@dataclass(frozen=True)
class User:
    login: str
    display_name: str | None = None
    is_bot: bool = False


@dataclass(frozen=True)
class CommitInfo:
    revision: str
    message: str | None
    authors: tuple[User, ...]
    committed_at: datetime | None


@dataclass(frozen=True)
class Comment:
    id: str
    author: User | None
    body: str
    created_at: datetime
    url: str | None
    is_hidden: bool = False
    diff_hunk: str | None = None


@dataclass(frozen=True)
class ReviewThread:
    id: str
    is_resolved: bool
    anchor: Anchor | None
    comments: tuple[Comment, ...]


@dataclass(frozen=True)
class InlineComment:
    body: str
    anchor: Anchor


@dataclass(frozen=True)
class Review:
    id: str
    author: User | None
    state: ReviewState
    body: str
    submitted_at: datetime | None
    revision: str | None
    comments: tuple[InlineComment, ...]


@dataclass(frozen=True)
class ReviewPostResult:
    """Outcome of ``Discussion.post_review``.

    ``reason`` is set by the host only when ``fell_back`` is true and carries the rejection text.
    """

    fell_back: bool
    review: Review | None
    comment: Comment | None
    reason: str | None = None


@dataclass(frozen=True)
class WorkItemRef:
    repository: RepositoryRef
    number: int


@dataclass(frozen=True)
class WorkItem:
    repository: RepositoryRef
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    assignees: tuple[User, ...]
    created_at: datetime
    state: WorkItemState


@dataclass(frozen=True)
class ChangeRequest:
    repository: RepositoryRef
    number: int
    title: str
    body: str
    url: str
    state: ChangeRequestState
    is_draft: bool
    author: User | None
    head_reference: str
    head_revision: str
    base_reference: str
    is_cross_repository: bool
    created_at: datetime
    mergeable: MergeableState | None = None
    auto_merge_enabled: bool = False
    review_decision: ReviewDecision | None = None
    requested_reviewers: tuple[User, ...] = ()
    additions: int | None = None
    deletions: int | None = None


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    details_url: str | None
    workflow_name: str | None
    started_at: datetime | None
    completed_at: datetime | None
    job_id: str | None


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str


@dataclass(frozen=True)
class Job:
    id: str
    failed_step: str | None
