import pytest

from dotharness.code_host import (
    Capability,
    ChangeRequest,
    CodeHostError,
    CodeHostTimeoutError,
    UnsupportedCapabilityError,
    User,
)
from dotharness.pr_review.runners.common import (
    DESIGN_REVIEW_MARKER,
    INLINE_REVIEW_MARKER,
    REGRET_REVIEW_MARKER,
    TRACEABILITY_REVIEW_MARKER,
    add_reviewer,
    author_matches,
    get_requested_reviewers,
    is_design_review_comment,
    is_inline_review_comment,
    is_review_summary_comment,
    is_traceability_review_comment,
    pr_from_url,
    preserve_reviewer_request,
    was_review_requested,
)
from tests.pr_review.runners.approve_support import REPO, make_host, seed_pr, update_pr


def test_author_matches_star_allows_all():
    assert author_matches("anyone", "*") is True


def test_author_matches_list_rejects_unknown():
    assert author_matches("eve", ["alice", "bob"]) is False


def test_author_matches_list_allows_known():
    assert author_matches("alice", ["alice", "bob"]) is True


@pytest.mark.parametrize(
    ("logins", "expected"),
    [
        (["alice", "bob"], ["alice", "bob"]),
        (["alice"], ["alice"]),
        ([], []),
    ],
)
async def test_get_requested_reviewers_returns_logins(logins, expected):
    host = make_host()
    seed_pr(host, 1, requested_reviewers=tuple(User(login) for login in logins))
    assert (await get_requested_reviewers(host.code_host(), REPO, 1)) == expected


async def test_get_requested_reviewers_returns_empty_on_host_failure():
    host = make_host()
    seed_pr(host, 1, requested_reviewers=(User("alice"),))
    host.fail_next("ChangeRequests.get", CodeHostError("boom"))
    assert (await get_requested_reviewers(host.code_host(), REPO, 1)) == []


async def test_get_requested_reviewers_unsupported_capability_warns_and_returns_empty(caplog):
    """Policy row (reviewer_request, ChangeRequests.get): warn and continue without the capability."""
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    with caplog.at_level("WARNING"):
        assert (await get_requested_reviewers(host.code_host(), REPO, 1)) == []
    assert "ChangeRequests" in caplog.text


async def test_was_review_requested_true_only_for_a_requested_login():
    host = make_host()
    seed_pr(host, 1, requested_reviewers=(User("alice"),))
    assert await was_review_requested(host.code_host(), REPO, 1, "alice") is True
    assert await was_review_requested(host.code_host(), REPO, 1, "bob") is False


async def test_pr_from_url_parses_number_and_fetches_the_change_request():
    host = make_host()
    seed_pr(host, 8484, title="T")
    pr = await pr_from_url(host.code_host(), REPO, "https://github.com/acme/frontend/pull/8484/")
    assert isinstance(pr, ChangeRequest)
    assert (pr.number, pr.title, pr.head_reference, pr.base_reference, pr.head_revision) == (
        8484,
        "T",
        "feat-8484",
        "main",
        "abc123",
    )
    assert [(c.operation, c.args) for c in host.calls] == [("ChangeRequests.get", (REPO, 8484))]


async def test_pr_from_url_returns_none_on_host_failure(caplog):
    host = make_host()
    seed_pr(host, 999)
    host.fail_next("ChangeRequests.get", CodeHostError("auth failed"))
    assert await pr_from_url(host.code_host(), REPO, "https://github.com/acme/repo/pull/999") is None
    assert "Failed to fetch PR #999: auth failed" in caplog.text


async def test_pr_from_url_returns_none_for_unknown_change_request():
    assert await pr_from_url(make_host().code_host(), REPO, "https://github.com/acme/repo/pull/1") is None


async def test_pr_from_url_unsupported_capability_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    with pytest.raises(UnsupportedCapabilityError):
        await pr_from_url(host.code_host(), REPO, "https://github.com/acme/repo/pull/1")


async def test_add_reviewer_requests_the_login():
    host = make_host()
    seed_pr(host, 1)
    await add_reviewer(host.code_host(), REPO, 1, "alice")
    assert [(c.operation, c.args) for c in host.writes] == [("ChangeRequests.request_reviewer", (REPO, 1, "alice"))]


async def test_add_reviewer_logs_and_swallows_a_failure(caplog):
    host = make_host()
    seed_pr(host, 1)
    host.fail_next("ChangeRequests.request_reviewer", CodeHostError("not found"))
    await add_reviewer(host.code_host(), REPO, 1, "alice")
    assert "failed to re-add alice as a reviewer on PR #1 (acme/frontend): not found" in caplog.text


async def test_add_reviewer_timeout_is_logged_and_never_retried(caplog):
    host = make_host()
    seed_pr(host, 1)
    host.fail_next("ChangeRequests.request_reviewer", CodeHostTimeoutError("slow", write=True))
    await add_reviewer(host.code_host(), REPO, 1, "alice")
    assert [c.operation for c in host.calls].count("ChangeRequests.request_reviewer") == 1
    assert "failed to re-add alice" in caplog.text


async def test_add_reviewer_unsupported_capability_warns_and_continues(caplog):
    """Policy row (reviewer_request, ChangeRequests.request_reviewer): warn."""
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    with caplog.at_level("WARNING"):
        await add_reviewer(host.code_host(), REPO, 1, "alice")
    assert "not supported" in caplog.text


def _requested(host) -> list[str]:
    return [u.login for u in host.change_requests[REPO][0].requested_reviewers]


async def test_preserve_reviewer_request_re_adds_a_previously_requested_login():
    host = make_host()
    seed_pr(host, 1, requested_reviewers=(User("alice"),))
    async with preserve_reviewer_request(host.code_host(), REPO, 1, "alice"):
        update_pr(host, 1, requested_reviewers=())  # submitting a review clears the request
        assert _requested(host) == []
    assert _requested(host) == ["alice"]


async def test_preserve_reviewer_request_re_adds_even_when_the_block_raises():
    host = make_host()
    seed_pr(host, 1, requested_reviewers=(User("alice"),))
    with pytest.raises(RuntimeError):
        async with preserve_reviewer_request(host.code_host(), REPO, 1, "alice"):
            update_pr(host, 1, requested_reviewers=())
            raise RuntimeError("boom")
    assert _requested(host) == ["alice"]


async def test_preserve_reviewer_request_does_nothing_when_not_previously_requested():
    host = make_host()
    seed_pr(host, 1)
    async with preserve_reviewer_request(host.code_host(), REPO, 1, "alice"):
        pass
    assert "ChangeRequests.request_reviewer" not in [c.operation for c in host.calls]


async def test_preserve_reviewer_request_skips_the_check_for_a_blank_login():
    host = make_host()
    seed_pr(host, 1)
    async with preserve_reviewer_request(host.code_host(), REPO, 1, ""):
        pass
    assert host.calls == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Review Summary\nNo blocking issues found.", True),
        ("# review summary\nlowercase heading still counts", True),
        ("## REVIEW SUMMARY", True),
        ("Review summary without a leading hash", True),
        ("# Some title\n\nReview Summary: no issues", True),
        ("dotharness-review passed", True),
        ("# Just a heading about something else", False),
        ("", False),
        ("   \n\n  ", False),
    ],
)
def test_is_review_summary_comment(body, expected):
    assert is_review_summary_comment(body) is expected


@pytest.mark.parametrize(
    "marker", [INLINE_REVIEW_MARKER, DESIGN_REVIEW_MARKER, TRACEABILITY_REVIEW_MARKER, REGRET_REVIEW_MARKER]
)
def test_is_review_summary_comment_excludes_pr_level_pass_markers(marker):
    # Every PR-level pass marker embeds the `dotharness-review` substring the summary
    # predicate looks for. A bare marker-only body must not be mistaken for the
    # file/summary pass's own completion comment (it would otherwise short-circuit
    # correctness review — see the traceability pass's "no linked ticket" comment below).
    assert is_review_summary_comment(marker) is False


def test_is_review_summary_comment_excludes_traceability_no_ticket_comment():
    body = (
        "# Requirement Traceability\n"
        "No linked ticket found (no closing-keyword link and no issue reference in the "
        "first 5 minutes of comments) — skipping scope/gap comparison.\n"
        f"{TRACEABILITY_REVIEW_MARKER}"
    )
    assert is_review_summary_comment(body) is False


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Nitpick: rename this variable.<!-- dotharness-review-inline -->", True),
        ("<!-- dotharness-review-inline -->", True),
        ("Nitpick: rename this variable.", False),
        ("**S1234** Refactor this to reduce complexity.", False),
        ("", False),
    ],
)
def test_is_inline_review_comment(body, expected):
    assert is_inline_review_comment(body) is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Design Review\nNo blocking design/architecture issues found.<!-- dotharness-review-design -->", True),
        ("<!-- dotharness-review-design -->", True),
        ("# Design Review\nNo blocking design/architecture issues found.", False),
        ("# Review Summary\nNo blocking issues found.", False),
        ("Nitpick: rename this variable.<!-- dotharness-review-inline -->", False),
        ("", False),
    ],
)
def test_is_design_review_comment(body, expected):
    assert is_design_review_comment(body) is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "# Requirement Traceability\nNo scope creep or requirement gaps found.<!-- dotharness-review-traceability -->",
            True,
        ),
        ("<!-- dotharness-review-traceability -->", True),
        ("# Requirement Traceability\nNo linked ticket found.", False),
        ("# Design Review\n...<!-- dotharness-review-design -->", False),
        ("", False),
    ],
)
def test_is_traceability_review_comment(body, expected):
    assert is_traceability_review_comment(body) is expected
