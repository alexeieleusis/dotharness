import pytest

from dotharness.code_host import (
    Capability,
    CodeHostError,
    CodeHostTimeoutError,
    ReviewState,
    UnsupportedCapabilityError,
)
from dotharness.pr_review.runners.auto_approve_github import GateStatus, check_timing_gate
from dotharness.pr_review.runners.common import (
    DESIGN_REVIEW_MARKER,
    INLINE_REVIEW_MARKER,
    REGRET_REVIEW_MARKER,
    TRACEABILITY_REVIEW_MARKER,
)
from tests.pr_review.runners.approve_support import (
    AUTHOR,
    EARLIER,
    LATER,
    ME,
    NUMBER,
    REPO,
    T,
    hour,
    make_host,
    put_comment,
    put_commit,
    put_review,
    put_thread,
    seed_pr,
)

SUMMARY = "## Review summary"
DESIGN = f"x {DESIGN_REVIEW_MARKER}"
TRACE = f"x {TRACEABILITY_REVIEW_MARKER}"
ALL_THREE = (SUMMARY, DESIGN, TRACE)


async def run(
    comments=(),
    inline=(),
    reviews=(),
    commits=(),
    *,
    author=AUTHOR,
    user=ME,
    setup=None,
    host=None,
):
    """comments: (login, body, at) change-request comments. inline: the same, one review thread each.
    reviews: (login, state, at). commits: (logins, at)."""
    host = host or make_host()
    seed_pr(host)
    for c in comments:
        put_comment(host, c[0], c[1], c[2], bot=len(c) > 3 and c[3])
    for c in inline:
        put_thread(host, [c])
    for r in reviews:
        put_review(host, *r)
    for logins, at in commits:
        put_commit(host, logins, at)
    ch = host.code_host()
    if setup:
        setup(host, ch)
    result = await check_timing_gate(ch, REPO, NUMBER, author, user)
    assert host.writes == []
    return result


def trio(at=T, login=ME):
    return [(login, body, at) for body in ALL_THREE]


def finding(at=T, login=ME, marker=INLINE_REVIEW_MARKER):
    return (login, f"finding {marker}", at)


@pytest.mark.parametrize("present", [[], [0], [0, 1], [1, 2], [0, 2]])
async def test_missing_any_of_three_not_passed(present):
    r = await run([trio()[i] for i in present])
    assert r.status is GateStatus.NOT_PASSED


async def test_zero_findings_passes():
    r = await run(trio())
    assert r.status is GateStatus.PASSED
    assert r.reason == "zero-findings"


async def test_regret_comment_ignored_for_t():
    regret = (ME, f"x {REGRET_REVIEW_MARKER}", LATER)
    r = await run([*trio(), regret, (AUTHOR, "later", hour(10, 30))], [finding()])
    assert r.status is GateStatus.PASSED
    assert r.reason == "author-activity-after-T"


@pytest.mark.parametrize("marker", [DESIGN_REVIEW_MARKER, TRACEABILITY_REVIEW_MARKER])
async def test_design_and_traceability_inline_comments_are_findings(marker):
    # Regression: inline comments tagged only with the design/traceability marker were not
    # counted, so the gate passed as "zero-findings" before the author responded.
    r = await run(trio(), [finding(LATER, marker=marker)])
    assert r.status is GateStatus.NOT_PASSED
    assert r.reason == "no author activity after T"


async def test_design_inline_comment_without_author_activity_not_passed():
    r = await run(trio(), [finding(marker=DESIGN_REVIEW_MARKER)])
    assert r.status is GateStatus.NOT_PASSED


async def test_author_activity_after_design_inline_comment_passes():
    r = await run([*trio(), (AUTHOR, "reply", LATER)], [finding(marker=DESIGN_REVIEW_MARKER)])
    assert r.status is GateStatus.PASSED
    assert r.reason == "author-activity-after-T"


async def test_inline_comments_count_for_t():
    # author comment is after the summary trio but before the newest inline comment
    r = await run([*trio(), (AUTHOR, "reply", hour(10, 30))], [finding(LATER)])
    assert r.status is GateStatus.NOT_PASSED


async def test_author_comment_passes():
    r = await run([*trio(), (AUTHOR, "ok", LATER)], [finding()])
    assert (r.status, r.reason) == (GateStatus.PASSED, "author-activity-after-T")


async def test_author_inline_reply_passes():
    reply = (AUTHOR, "fixed", LATER)
    assert (await run(trio(), [finding(), reply])).status is GateStatus.PASSED


async def test_author_review_uses_submitted_at():
    r = await run(trio(), [finding()], [(AUTHOR, ReviewState.COMMENTED, LATER)])
    assert r.status is GateStatus.PASSED
    pending = await run(trio(), [finding()], [(AUTHOR, ReviewState.PENDING, None)])
    assert pending.status is GateStatus.NOT_PASSED


async def test_author_commit_after_t_passes():
    r = await run(trio(), [finding()], commits=[([AUTHOR], LATER)])
    assert (r.status, r.reason) == (GateStatus.PASSED, "author-activity-after-T")


async def test_commit_by_other_author_ignored():
    assert (await run(trio(), [finding()], commits=[(["bob"], LATER)])).status is GateStatus.NOT_PASSED


async def test_commit_at_or_before_t_not_passed():
    assert (await run(trio(), [finding()], commits=[([AUTHOR], T)])).status is GateStatus.NOT_PASSED
    assert (await run(trio(), [finding()], commits=[([AUTHOR], EARLIER)])).status is GateStatus.NOT_PASSED


async def test_commit_without_a_date_is_ignored():
    assert (await run(trio(), [finding()], commits=[([AUTHOR], None)])).status is GateStatus.NOT_PASSED


async def test_equal_timestamp_not_passed():
    r = await run([*trio(), (AUTHOR, "same", T)], [finding()])
    assert r.status is GateStatus.NOT_PASSED


async def test_acting_account_and_bot_activity_ignored():
    items = [
        *trio(),
        (ME, "me", LATER),
        (AUTHOR, "bot", LATER, True),
        ("alice[bot]", "bot2", LATER),
        ("bob", "other", LATER),
    ]
    assert (await run(items, [finding()])).status is GateStatus.NOT_PASSED


@pytest.mark.parametrize("operation", ["Discussion.comments", "Discussion.review_threads", "ChangeRequests.reviews"])
@pytest.mark.parametrize("error", [CodeHostError("boom"), CodeHostTimeoutError("slow", write=False)])
async def test_read_failure_is_read_failed(operation, error):
    r = await run(trio(), [finding()], setup=lambda host, _ch: host.fail_next(operation, error))
    assert r.status is GateStatus.READ_FAILED
    assert r.reason.endswith("read failed")


async def test_commits_read_failure_is_read_failed():
    r = await run(
        trio(),
        [finding()],
        setup=lambda host, _ch: host.fail_next("ChangeRequests.commits", CodeHostError("boom")),
    )
    assert (r.status, r.reason) == (GateStatus.READ_FAILED, "commits read failed")


async def test_unknown_acting_account_is_read_failed_without_any_read():
    host = make_host()
    seed_pr(host)
    assert (await check_timing_gate(host.code_host(), REPO, NUMBER, AUTHOR, "")).status is GateStatus.READ_FAILED
    assert (await check_timing_gate(host.code_host(), REPO, NUMBER, "", ME)).status is GateStatus.READ_FAILED
    assert host.calls == []


@pytest.mark.parametrize("exc", [OSError("boom"), ValueError("bad")])
@pytest.mark.parametrize(("facet", "method"), [("discussion", "comments"), ("change_requests", "reviews")])
async def test_raised_read_error_is_read_failed(monkeypatch, facet, method, exc):
    def raiser(*_args, **_kwargs):
        raise exc

    def setup(_host, ch):
        monkeypatch.setattr(getattr(ch, facet), method, raiser)

    assert (await run(trio(), [finding()], setup=setup)).status is GateStatus.READ_FAILED


@pytest.mark.parametrize("capability", [Capability.DISCUSSION, Capability.CHANGE_REQUESTS])
async def test_unsupported_capability_stops(capability):
    host = make_host(capabilities=frozenset(Capability) - {capability})
    seed_pr(host)
    with pytest.raises(UnsupportedCapabilityError) as exc:
        await check_timing_gate(host.code_host(), REPO, NUMBER, AUTHOR, ME)
    assert exc.value.capability == capability.value


async def test_author_login_matched_case_insensitively():
    assert (await run([*trio(), ("Alice", "ok", LATER)], [finding()])).status is GateStatus.PASSED
    assert (await run(trio(), [finding()], commits=[(["Alice"], LATER)])).status is GateStatus.PASSED


async def test_acting_login_matched_case_insensitively():
    # Mixed-case acting-account comments still count as the tool's own review comments...
    own = trio(login="Reviewer")
    assert (await run(own, [finding(login="Reviewer")])).status is GateStatus.NOT_PASSED
    # ...and its later activity is not author activity even when it is also the PR author.
    later = ("Reviewer", "me", LATER)
    r = await run([*own, later], [finding(login="Reviewer")], author="reviewer")
    assert r.status is GateStatus.NOT_PASSED


async def test_items_with_no_author_do_not_raise():
    ghost = (None, "hi", LATER)
    r = await run([*trio(), ghost], [finding(), ghost], [(None, ReviewState.COMMENTED, LATER)])
    assert r.status is GateStatus.NOT_PASSED
