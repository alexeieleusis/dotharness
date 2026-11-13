"""Traceability readers of ``common.py`` on the in-memory host: linked work items, early comments, prompt text.

Classification: converted. Exact ``gh`` arguments of ``WorkItems.get`` / ``linked_work_items`` live in
``tests/code_host/test_reference*.py``.
"""

from datetime import UTC, datetime, timedelta

import pytest

from dotharness.code_host import (
    Capability,
    CodeHostError,
    CodeHostTimeoutError,
    RepositoryRef,
    UnsupportedCapabilityError,
)
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.pr_review.runners.common import (
    LinkedTicket,
    build_design_review_prompt,
    build_early_comment_context,
    build_traceability_review_prompt,
    resolve_linked_tickets,
)
from tests.pr_review.runners.approve_support import REPO, make_host, put_comment, seed_pr, update_pr

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NUMBER = 39
OTHER = RepositoryRef("someorg/othereproj")


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _host(**kwargs) -> InMemoryCodeHost:
    host = make_host(**kwargs)
    seed_pr(host, NUMBER, created_at=T0)
    return host


def _pr(host: InMemoryCodeHost):
    return host._change_requests[REPO][0]


async def _resolve(host: InMemoryCodeHost, cache: dict | None = None):
    return await resolve_linked_tickets(host.code_host(), REPO, _pr(host), cache)


def _linear_linkback_body(ticket_id: str = "AGNT-1215", title: str = "Structured approval card") -> str:
    return (
        "<!-- linear-linkback -->\n<details>\n"
        f'<summary><a href="https://linear.app/acme/issue/{ticket_id}/slug">{ticket_id} {title}</a></summary>\n'
        "<p>\n\n## Problem\n\nSomething is broken.\n\n## Expected\n\nIt should work.\n</p>\n</details>\n"
        '<!-- linear-review-link -->\n<p><a href="https://linear.app/acme/review/xyz">Review in Linear</a></p>\n'
    )


# --- resolve_linked_tickets: native linking (WorkItems.linked_work_items) -----------------


async def test_native_link_single_ticket():
    host = _host()
    host.add_work_item(REPO, title="Do the thing", body="Body of 3", number=3)
    host.link_closing(REPO, work_item=3, change_request=NUMBER)
    tickets = await _resolve(host)
    assert tickets == [LinkedTicket(3, "acme/frontend", "Do the thing", "Body of 3", "closing_keyword")]
    assert [c.operation for c in host.calls if c.operation.startswith("Discussion")] == []


async def test_native_link_dedupes_by_repository_and_number():
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    host.link_closing(REPO, work_item=3, change_request=NUMBER)
    host.link_closing(REPO, work_item=3, change_request=NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert len(tickets) == 1


async def test_native_link_preserves_link_order():
    host = _host()
    for n in (5, 3):
        host.add_work_item(REPO, title=f"T{n}", number=n)
        host.link_closing(REPO, work_item=n, change_request=NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert [t.number for t in tickets] == [5, 3]


async def test_native_link_inconclusive_read_returns_none_and_skips_comment_scan():
    """A failed read of the linked work items must surface as None, not as "no ticket" - and the comment-scan
    fallback must not even run, since it would very likely also come up empty and let a real linked ticket get lost
    behind the terminal "no ticket" comment."""
    host = _host()
    put_comment(host, "alice", "Related: #3", _at(60), NUMBER)
    host.fail_next("WorkItems.linked_work_items", CodeHostError("rate limit"))
    assert await _resolve(host) is None
    assert not [c for c in host.calls if c.operation == "Discussion.comments"]


async def test_native_link_timeout_returns_none():
    host = _host()
    host.fail_next("WorkItems.linked_work_items", CodeHostTimeoutError("slow", write=False))
    assert await _resolve(host) is None


async def test_native_link_unsupported_capability_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.WORK_ITEMS})
    seed_pr(host, NUMBER, created_at=T0)
    with pytest.raises(UnsupportedCapabilityError):
        await _resolve(host)


async def test_native_link_without_links_falls_back_to_comment_scan():
    host = _host()
    assert await _resolve(host) == []
    assert [c.operation for c in host.calls].count("Discussion.comments") == 1


# --- resolve_linked_tickets: comment-scan fallback ------------------------------------------


async def test_comment_scan_same_repository_reference():
    host = _host()
    host.add_work_item(REPO, title="Do the thing", body="Ticket body", number=3)
    put_comment(host, "alice", "Related ticket: #3", _at(60), NUMBER)
    assert await _resolve(host) == [LinkedTicket(3, "acme/frontend", "Do the thing", "Ticket body", "comment")]


async def test_comment_scan_full_url_reference():
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    put_comment(host, "alice", "See https://github.com/acme/frontend/issues/3 for context", _at(60), NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert [(t.number, t.source) for t in tickets] == [(3, "comment")]


async def test_comment_scan_cross_repository_shorthand_resolves_the_named_repository():
    host = _host()
    host.add_work_item(OTHER, title="Elsewhere", body="b", number=42)
    put_comment(host, "alice", "Tracked in someorg/othereproj#42", _at(60), NUMBER)
    assert await _resolve(host) == [LinkedTicket(42, "someorg/othereproj", "Elsewhere", "b", "comment")]
    assert any(c.operation == "Repository.resolve" for c in host.calls)


async def test_comment_scan_drops_reference_to_a_pull_request_or_absent_item():
    host = _host()
    put_comment(host, "alice", "Superseded by #40", _at(60), NUMBER)
    assert await _resolve(host) == []


async def test_comment_scan_drops_reference_whose_read_fails():
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    put_comment(host, "alice", "See #3", _at(60), NUMBER)
    host.fail_next("WorkItems.get", CodeHostError("rate limit"))
    assert await _resolve(host) == []


async def test_comment_scan_unsupported_work_items_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.WORK_ITEMS})
    seed_pr(host, NUMBER, created_at=T0)
    put_comment(host, "alice", "See #3", _at(60), NUMBER)
    with pytest.raises(UnsupportedCapabilityError):
        await _resolve(host)


async def test_comment_scan_includes_bot_authored_comments():
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    put_comment(host, "jira-bridge[bot]", "Linked: #3", _at(60), NUMBER, bot=True)
    tickets = await _resolve(host)
    assert tickets is not None
    assert [t.number for t in tickets] == [3]


async def test_comment_scan_excludes_comments_outside_window():
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    put_comment(host, "alice", "Too late: #3", _at(301), NUMBER)
    assert await _resolve(host) == []


async def test_comment_scan_includes_comment_at_the_end_of_the_window():
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    put_comment(host, "alice", "Just in time: #3", _at(300), NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert len(tickets) == 1


async def test_comment_scan_dedupes_repeated_references():
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    put_comment(host, "alice", "See #3", _at(60), NUMBER)
    put_comment(host, "alice", "Also #3", _at(120), NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert len(tickets) == 1
    assert [c.operation for c in host.calls].count("WorkItems.get") == 1


async def test_falls_back_to_linear_linkback_comment():
    host = _host()
    put_comment(host, "linear[bot]", _linear_linkback_body(), _at(60), NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert len(tickets) == 1
    ticket = tickets[0]
    assert (ticket.number, ticket.repo, ticket.title, ticket.source) == (
        "AGNT-1215",
        "Linear",
        "Structured approval card",
        "linear_linkback",
    )
    assert "## Problem" in ticket.body
    assert "Review in Linear" not in ticket.body
    assert "<details>" not in ticket.body


async def test_linear_linkback_does_not_also_scan_for_issue_refs():
    """A linkback comment's body can incidentally contain "#N"-shaped text; it must not be double-counted as an
    issue reference on top of the Linear ticket it already is."""
    host = _host()
    host.add_work_item(REPO, title="T", number=3)
    body = _linear_linkback_body().replace("Something is broken.", "Something is broken, see #3.")
    put_comment(host, "linear[bot]", body, _at(60), NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert [t.source for t in tickets] == ["linear_linkback"]
    assert "WorkItems.get" not in [c.operation for c in host.calls]


async def test_linear_linkback_dedupes_same_ticket():
    host = _host()
    put_comment(host, "linear[bot]", _linear_linkback_body(), _at(60), NUMBER)
    put_comment(host, "linear[bot]", _linear_linkback_body(), _at(120), NUMBER)
    tickets = await _resolve(host)
    assert tickets is not None
    assert len(tickets) == 1


async def test_marker_with_unrecognized_shape_is_no_ticket():
    host = _host()
    put_comment(host, "linear[bot]", "<!-- linear-linkback -->\nsomething unexpected", _at(60), NUMBER)
    assert await _resolve(host) == []


async def test_nothing_resolves_returns_empty_list():
    assert await _resolve(_host()) == []


async def test_comment_fetch_failure_in_scan_returns_empty_list():
    host = _host()
    host.fail_next("Discussion.comments", CodeHostError("boom"))
    assert await _resolve(host) == []


# --- build_early_comment_context -------------------------------------------------------------


async def _context(host: InMemoryCodeHost, cache: dict | None = None, created_at: datetime = T0) -> str:
    return await build_early_comment_context(host.code_host(), REPO, NUMBER, created_at, cache)


async def test_early_context_concatenates_bodies_within_window():
    host = _host()
    put_comment(host, "alice", "first note", _at(60), NUMBER)
    put_comment(host, "bob", "second note", _at(240), NUMBER)
    assert await _context(host) == "first note\n\nsecond note"


async def test_early_context_excludes_comments_after_window():
    host = _host()
    put_comment(host, "alice", "in window", _at(300), NUMBER)
    put_comment(host, "alice", "out of window", _at(301), NUMBER)
    assert await _context(host) == "in window"


async def test_early_context_skips_empty_bodies():
    host = _host()
    put_comment(host, "alice", "", _at(10), NUMBER)
    put_comment(host, "alice", "note", _at(20), NUMBER)
    assert await _context(host) == "note"


async def test_early_context_empty_when_no_comments():
    assert await _context(_host()) == ""


async def test_early_context_empty_on_fetch_failure():
    host = _host()
    put_comment(host, "alice", "note", _at(10), NUMBER)
    host.fail_next("Discussion.comments", CodeHostError("boom"))
    assert await _context(host) == ""


async def test_early_context_unsupported_discussion_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.DISCUSSION})
    seed_pr(host, NUMBER, created_at=T0)
    with pytest.raises(UnsupportedCapabilityError):
        await _context(host)


async def test_early_context_reads_every_comment_the_host_returns():
    host = _host()
    for i in range(101):
        put_comment(host, "alice", f"note {i}", _at(60), NUMBER)
    assert (await _context(host)).count("note") == 101


# --- shared cache ------------------------------------------------------------------------------


async def test_resolve_and_early_context_share_one_comment_fetch():
    """When both functions are given the same `cache` dict for the same PR, the comment fetch runs once."""
    host = _host()
    host.add_work_item(REPO, title="Do the thing", number=3)
    put_comment(host, "alice", "Related ticket: #3", _at(60), NUMBER)
    cache: dict = {}
    tickets = await _resolve(host, cache)
    context = await _context(host, cache)
    assert tickets is not None
    assert [t.number for t in tickets] == [3]
    assert context == "Related ticket: #3"
    assert [c.operation for c in host.calls].count("Discussion.comments") == 1


async def test_a_failed_fetch_is_cached_too():
    host = _host()
    host.fail_next("Discussion.comments", CodeHostError("boom"))
    cache: dict = {}
    assert await _resolve(host, cache) == []
    assert await _context(host, cache) == ""
    assert [c.operation for c in host.calls].count("Discussion.comments") == 1


# --- prompt builders: byte-identical text (FR-A4) -------------------------------------------


def _model(host: InMemoryCodeHost):
    return update_pr(host, NUMBER, url="https://github.com/acme/frontend/pull/39")


_TRACEABILITY_EXPECTED = (
    "INSTRUCTIONS"
    "\n\n## Additional Review Guide\nGUIDE"
    "\n\n## Linked Ticket(s)\n"
    "### #3 (acme/frontend) — source: closing_keyword\n**Title:** Do the thing\n\nBody of 3"
    "\n\n"
    "### #AGNT-1 (Linear) — source: linear_linkback\n**Title:** Card\n\nLinear body"
    "\n\n## Early PR Comments\nearly note"
    "\n\n## File: a.py\ndiff a"
    "\n\n## Already-flagged scope-creep findings\n"
    "The following file/line locations already have an inline traceability-review comment from a previous attempt "
    "on this PR. Do not post a new inline comment for any of them:\n- a.py:10\n- b.py:2"
    "\n\nPR URL: https://github.com/acme/frontend/pull/39\nPR number: 39\nRepo: acme/frontend\nCommit: abc123"
    "\n\n## PR Description\n**Title:** T\n\nB"
    "\n\n## Static Analysis\nsonar says no"
)


def test_traceability_prompt_text_is_byte_identical():
    host = _host()
    prompt = build_traceability_review_prompt(
        "INSTRUCTIONS",
        "GUIDE",
        "\n\n## File: a.py\ndiff a",
        _model(host),
        NUMBER,
        "acme/frontend",
        "abc123",
        "## PR Description\n**Title:** T\n\nB",
        "sonar says no",
        [("a.py", 10), ("b.py", 2)],
        [
            LinkedTicket(3, "acme/frontend", "Do the thing", "Body of 3", "closing_keyword"),
            LinkedTicket("AGNT-1", "Linear", "Card", "Linear body", "linear_linkback"),
        ],
        "early note",
    )
    assert prompt == _TRACEABILITY_EXPECTED


def test_traceability_prompt_minimal_inputs():
    host = _host()
    prompt = build_traceability_review_prompt(
        "I",
        None,
        "",
        _model(host),
        NUMBER,
        "acme/frontend",
        "abc",
        None,
        None,
        [],
        [LinkedTicket(1, "r", "t", "b", "comment")],
        "",
    )
    assert prompt == (
        "I\n\n## Linked Ticket(s)\n### #1 (r) — source: comment\n**Title:** t\n\nb"
        "\n\nPR URL: https://github.com/acme/frontend/pull/39\nPR number: 39\nRepo: acme/frontend\nCommit: abc"
    )


_DESIGN_EXPECTED = (
    "DESIGN"
    "\n\n## Additional Review Guide\nGUIDE"
    "\n\n## File: a.py\ndiff a"
    "\n\n## Already-flagged design findings\n"
    "The following file/line locations already have an inline design-review comment from a previous attempt on this "
    "PR. Do not post a new inline comment for any of them:\n- a.py:10"
    "\n\nPR URL: https://github.com/acme/frontend/pull/39\nPR number: 39\nRepo: acme/frontend\nCommit: abc123"
    "\n\n## PR Description\n**Title:** T\n\nB"
    "\n\n## Static Analysis\nsonar says no"
)


def test_design_prompt_text_is_byte_identical():
    host = _host()
    prompt = build_design_review_prompt(
        "DESIGN",
        "GUIDE",
        "\n\n## File: a.py\ndiff a",
        _model(host),
        NUMBER,
        "acme/frontend",
        "abc123",
        "## PR Description\n**Title:** T\n\nB",
        "sonar says no",
        [("a.py", 10)],
    )
    assert prompt == _DESIGN_EXPECTED


def test_design_prompt_minimal_inputs():
    host = _host()
    prompt = build_design_review_prompt("D", None, "", _model(host), NUMBER, "acme/frontend", "abc", None, None, [])
    assert prompt == (
        "D\n\nPR URL: https://github.com/acme/frontend/pull/39\nPR number: 39\nRepo: acme/frontend\nCommit: abc"
    )
