"""Shared scaffold for runner tests that patch ``open_host`` (the host, repository and environment a runner builds)."""

import os
from dataclasses import replace
from datetime import UTC, datetime

from dotharness.code_host import ChangeRequest, ChangeRequestState, RepositoryRef, User
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.pr_review.runners.common import HostScaffold

REPO = RepositoryRef("acme/frontend")


def make_scaffold(host: InMemoryCodeHost | None = None, token: str = "tok", **env: str) -> HostScaffold:  # noqa: S107
    """A ``HostScaffold`` on an in-memory host; ``env`` is what the runner passes to git and agents."""
    return HostScaffold(
        host=(host or InMemoryCodeHost(repository="acme/frontend", user="alice")).code_host(),
        repo=REPO,
        env={**os.environ, "GITHUB_TOKEN": token, **env},
    )


def make_change_request(number: int = 1, **fields) -> ChangeRequest:
    """A change request on ``acme/frontend``; ``fields`` replace any ChangeRequest field."""
    base = ChangeRequest(
        repository=REPO,
        number=number,
        title="T",
        body="B",
        url=f"https://example.test/acme/frontend/pull/{number}",
        state=ChangeRequestState.OPEN,
        is_draft=False,
        author=User("alice"),
        head_reference=f"feat-{number}",
        head_revision=f"sha{number}",
        base_reference="main",
        is_cross_repository=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return replace(base, **fields)
