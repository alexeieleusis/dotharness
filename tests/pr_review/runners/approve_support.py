"""Seeding helpers shared by the ``auto_approve_github`` tests: explicit timestamps on the in-memory host.

The fake stamps objects with a counter in insertion order; the timing gate compares against explicit times, so these
helpers append objects with the timestamp the test names. They edit the fake's state views directly, as the other
runner tests do.
"""

from dataclasses import replace
from datetime import UTC, datetime

from dotharness.code_host import (
    Anchor,
    ChangeRequest,
    Comment,
    CommitInfo,
    MergeableState,
    RepositoryRef,
    Review,
    ReviewState,
    ReviewThread,
    User,
)
from dotharness.code_host.testing import InMemoryCodeHost

REPO = RepositoryRef("acme/frontend")
ME = "reviewer"
AUTHOR = "alice"
HEAD = "abc123"
NUMBER = 5


def hour(h: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 6, h, minute, tzinfo=UTC)


T = hour(10)
LATER = hour(11)
EARLIER = hour(9)


def make_host(**kwargs) -> InMemoryCodeHost:
    return InMemoryCodeHost(repository="acme/frontend", user=ME, **kwargs)


def seed_pr(host: InMemoryCodeHost, number: int = NUMBER, *, head: str = HEAD, **fields) -> ChangeRequest:
    """An open, mergeable change request authored by ``alice``; ``fields`` replace any ChangeRequest field."""
    host.add_change_request(REPO, head_reference=f"feat-{number}", number=number, head_revision=head, author=AUTHOR)
    return update_pr(host, number, **{"mergeable": MergeableState.MERGEABLE, **fields})


def update_pr(host: InMemoryCodeHost, number: int, **fields) -> ChangeRequest:
    items = host._change_requests[REPO]
    for i, item in enumerate(items):
        if item.number == number:
            items[i] = replace(item, **fields)
            host._version += 1
            return items[i]
    raise AssertionError(number)


def _comment(host: InMemoryCodeHost, login: str | None, body: str, at: datetime, *, bot: bool = False) -> Comment:
    author = None if login is None else User(login, is_bot=bot)
    return Comment(id=host._next_id(), author=author, body=body, created_at=at, url=None)


def put_comment(host, login, body, at, number=NUMBER, *, bot=False) -> Comment:
    comment = _comment(host, login, body, at, bot=bot)
    host._comments.setdefault(REPO, {}).setdefault(number, []).append(comment)
    host._version += 1
    return comment


def put_thread(host, comments, number=NUMBER) -> ReviewThread:
    """One review thread holding ``comments`` ((login, body, at) tuples)."""
    thread = ReviewThread(
        id=f"thread-{host._next_id()}",
        is_resolved=False,
        anchor=Anchor("a.py", 1, "RIGHT"),
        comments=tuple(_comment(host, *c) for c in comments),
    )
    host._threads.setdefault(REPO, {}).setdefault(number, []).append(thread)
    host._version += 1
    return thread


def put_review(host, login, state, at, number=NUMBER, *, revision=HEAD) -> Review:
    review = Review(
        id=host._next_id(),
        author=None if login is None else User(login),
        state=state,
        body="",
        submitted_at=at,
        revision=revision,
        comments=(),
    )
    host._reviews.setdefault(REPO, {}).setdefault(number, []).append(review)
    host._version += 1
    return review


def put_approval(host, login=ME, number=NUMBER, *, revision=HEAD, state=ReviewState.APPROVED) -> Review:
    return put_review(host, login, state, T, number, revision=revision)


def put_commit(host, logins, at, number=NUMBER) -> CommitInfo:
    commit = CommitInfo(
        revision=host._next_id(), message=None, authors=tuple(User(login) for login in logins), committed_at=at
    )
    host._commits.setdefault(REPO, {}).setdefault(number, []).append(commit)
    host._version += 1
    return commit


def mutating(host: InMemoryCodeHost) -> list[str]:
    return [c.operation for c in host.writes]
