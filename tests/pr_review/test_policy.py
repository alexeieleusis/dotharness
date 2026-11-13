"""Unsupported-capability policy: the constant, its documentation and the three behaviours."""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from dotharness.code_host import (
    Capability,
    CodeHost,
    CodeHostError,
    RepositoryRef,
    UnsupportedCapabilityError,
)
from dotharness.code_host.testing import SECRET, InMemoryCodeHost
from dotharness.pr_review.runners.common import (
    UNSUPPORTED_CAPABILITY_POLICY,
    call_with_policy,
    unsupported_policy,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "pr_review" / "configuration.md"
REPO = RepositoryRef("example/repo")

# Concrete operation to exercise for each table row (``Checks.*`` is exercised through ``list_checks``).
_CALLS: dict[str, Callable[[CodeHost], Awaitable[Any]]] = {
    "ChangeRequests.squash_merge": lambda h: h.change_requests.squash_merge(REPO, 1, expected_head="x"),
    "ChangeRequests.get": lambda h: h.change_requests.get(REPO, 1),
    "ChangeRequests.approve": lambda h: h.change_requests.approve(REPO, 1, head_revision="x"),
    "ChangeRequests.reviews": lambda h: h.change_requests.reviews(REPO, 1),
    "ChangeRequests.commits": lambda h: h.change_requests.commits(REPO, 1),
    "ChangeRequests.viewed_files": lambda h: h.change_requests.viewed_files(REPO, 1),
    "ChangeRequests.mark_viewed": lambda h: h.change_requests.mark_viewed(REPO, 1, "a.py"),
    "Checks.*": lambda h: h.checks.list_checks(REPO, 1),
    "Checks.list_workflows": lambda h: h.checks.list_workflows(REPO),
    "Checks.list_checks": lambda h: h.checks.list_checks(REPO, 1, required_only=True),
    "ChangeRequests.request_reviewer": lambda h: h.change_requests.request_reviewer(REPO, 1, "bob"),
    "ChangeRequests.find_by_commit": lambda h: h.change_requests.find_by_commit(REPO, "abc"),
    "WorkItems.get": lambda h: h.work_items.get(REPO, 1),
    "WorkItems.linked_work_items": lambda h: h.work_items.linked_work_items(REPO, 1),
    "Session.current_user": lambda h: h.session.current_user(),
}

_ROWS = sorted(UNSUPPORTED_CAPABILITY_POLICY.items())


def _row_id(row: tuple[tuple[str, str], str]) -> str:
    (site, operation), policy = row
    return f"{site}-{operation}-{policy}"


def _capability(operation: str) -> Capability:
    return Capability(operation.split(".", 1)[0])


def _call_for(operation: str) -> Callable[[CodeHost], Awaitable[Any]]:
    return _CALLS[operation]


def test_table_values_are_the_three_policies() -> None:
    assert set(UNSUPPORTED_CAPABILITY_POLICY.values()) == {"degrade", "warn", "stop"}


def test_table_matches_the_confirmed_policy() -> None:
    sites = {
        site: {p for (s, _), p in UNSUPPORTED_CAPABILITY_POLICY.items() if s == site}
        for site in {s for s, _ in UNSUPPORTED_CAPABILITY_POLICY}
    }
    assert sites == {
        "merge": {"stop"},
        "auto_approve": {"stop"},
        "auto_approve_viewed": {"degrade"},
        "ci_fix": {"stop"},
        "ci_checks_workflows": {"degrade"},
        "required_checks": {"stop"},
        "reviewer_request": {"warn"},
        "regret_review": {"stop"},
        "traceability": {"stop"},
        "current_user": {"warn"},
    }


def test_unlisted_pair_is_stop() -> None:
    assert unsupported_policy("regret_review", "Discussion.post_comment") == "stop"
    assert unsupported_policy("review_prs", "ChangeRequests.list") == "stop"


def test_wildcard_covers_every_operation_of_the_capability() -> None:
    assert unsupported_policy("ci_fix", "Checks.list_jobs") == "stop"
    assert unsupported_policy("ci_fix", "ChangeRequests.get") == "stop"  # unlisted, default


@pytest.mark.parametrize("row", _ROWS, ids=_row_id)
async def test_each_row_behaves_as_declared(row: tuple[tuple[str, str], str], caplog: pytest.LogCaptureFixture) -> None:
    (site, operation), policy = row
    capability = _capability(operation)
    host = InMemoryCodeHost(capabilities=frozenset(Capability) - {capability})
    call = _call_for(operation)
    sentinel = object()
    caplog.set_level(logging.WARNING, logger="dotharness.pr_review.runners.common")

    if policy == "stop":
        with pytest.raises(UnsupportedCapabilityError) as info:
            await call_with_policy(site, operation, lambda: call(host.code_host()), sentinel)
        assert info.value.capability == capability.value
        assert not caplog.records
    elif policy == "degrade":
        assert await call_with_policy(site, operation, lambda: call(host.code_host()), sentinel) is sentinel
        assert not caplog.records
    else:
        assert await call_with_policy(site, operation, lambda: call(host.code_host()), sentinel) is sentinel
        assert [capability.value in r.getMessage() for r in caplog.records] == [True]
        assert all(r.levelno == logging.WARNING for r in caplog.records)
    assert host.writes == []


@pytest.mark.parametrize("row", _ROWS, ids=_row_id)
async def test_each_row_with_injected_unsupported_error(row: tuple[tuple[str, str], str]) -> None:
    (site, operation), policy = row
    concrete = "Checks.list_checks" if operation == "Checks.*" else operation
    host = InMemoryCodeHost()
    host.fail_next(concrete, UnsupportedCapabilityError("nope", capability=concrete.split(".")[0]))
    sentinel = object()
    call = _call_for(operation)
    if policy == "stop":
        with pytest.raises(UnsupportedCapabilityError):
            await call_with_policy(site, operation, lambda: call(host.code_host()), sentinel)
    else:
        assert await call_with_policy(site, operation, lambda: call(host.code_host()), sentinel) is sentinel


@pytest.mark.parametrize("policy_site", ["auto_approve_viewed", "reviewer_request", "traceability"])
async def test_other_host_errors_are_not_handled(policy_site: str) -> None:
    host = InMemoryCodeHost()
    operation = {
        "auto_approve_viewed": "ChangeRequests.viewed_files",
        "reviewer_request": "ChangeRequests.request_reviewer",
        "traceability": "WorkItems.get",
    }[policy_site]
    host.fail_next(operation, CodeHostError("boom"))
    call = _call_for(operation)
    with pytest.raises(CodeHostError) as info:
        await call_with_policy(policy_site, operation, lambda: call(host.code_host()), None)
    assert not isinstance(info.value, UnsupportedCapabilityError)


async def test_success_returns_the_call_result_and_does_not_log(caplog: pytest.LogCaptureFixture) -> None:
    host = InMemoryCodeHost()
    caplog.set_level(logging.DEBUG)
    user = await call_with_policy("current_user", "Session.current_user", host.code_host().session.current_user, "")
    assert user == "fake-user"
    assert not caplog.records
    assert [c.operation for c in host.calls] == ["Session.current_user"]


async def test_warn_log_holds_no_secret(caplog: pytest.LogCaptureFixture) -> None:
    host = InMemoryCodeHost(capabilities=frozenset(Capability) - {Capability.SESSION})
    caplog.set_level(logging.DEBUG)
    result = await call_with_policy("current_user", "Session.current_user", host.code_host().session.current_user, "")
    assert result == ""
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Session" in text
    assert SECRET not in text


def test_doc_table_lists_every_code_row() -> None:
    doc = DOC.read_text()
    assert "## Unsupported-capability policy" in doc
    for (site, operation), policy in UNSUPPORTED_CAPABILITY_POLICY.items():
        assert f"| `{site}` | `{operation}` | {policy} |" in doc, (site, operation)


def test_doc_table_has_no_extra_rows() -> None:
    doc = DOC.read_text()
    section = doc.split("## Unsupported-capability policy", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("| `")]
    assert len(rows) == len(UNSUPPORTED_CAPABILITY_POLICY)


def test_doc_names_the_three_behaviours() -> None:
    doc = DOC.read_text()
    for word in ("**degrade**", "**warn**", "**stop**"):
        assert word in doc
