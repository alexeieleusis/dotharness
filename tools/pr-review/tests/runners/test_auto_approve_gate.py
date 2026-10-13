import json
from unittest.mock import MagicMock, patch

import pytest

from harness.runners.auto_approve_github import GateStatus, check_timing_gate
from harness.runners.common import (
    DESIGN_REVIEW_MARKER,
    INLINE_REVIEW_MARKER,
    REGRET_REVIEW_MARKER,
    TRACEABILITY_REVIEW_MARKER,
)

ME = "reviewer"
AUTHOR = "alice"
T = "2026-10-06T10:00:00Z"
LATER = "2026-10-06T11:00:00Z"
EARLIER = "2026-10-06T09:00:00Z"


def issue(body, login=ME, ts=T, utype="User"):
    return {"body": body, "created_at": ts, "user": {"login": login, "type": utype}}


def summary(ts=T):
    return issue("## Review summary", ts=ts)


def design(ts=T):
    return issue(f"x {DESIGN_REVIEW_MARKER}", ts=ts)


def trace(ts=T):
    return issue(f"x {TRACEABILITY_REVIEW_MARKER}", ts=ts)


def all_three(ts=T):
    return [summary(ts), design(ts), trace(ts)]


def inline(ts=T, login=ME):
    return issue(f"finding {INLINE_REVIEW_MARKER}", login=login, ts=ts)


def commits_json(date, login=AUTHOR):
    return json.dumps({"commits": [{"committedDate": date, "authors": [{"login": login}]}]}).encode()


def run(issues, review_comments, reviews, commits=None):
    """commits: bytes stdout, or None for a failed read."""
    pages = {"issues": issues, "pulls": review_comments, "reviews": reviews}

    def fake_pages(path, env):
        key = "issues" if "/issues/" in path else "reviews" if path.endswith("/reviews") else "pulls"
        return pages[key]

    if commits is None:
        proc = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
    else:
        proc = MagicMock(returncode=0, stdout=commits, stderr=b"")
    with (
        patch("harness.runners.auto_approve_github.fetch_all_pages", side_effect=fake_pages),
        patch("harness.runners.auto_approve_github.run_cmd", return_value=proc) as mock_cmd,
    ):
        result = check_timing_gate("o/r", 7, AUTHOR, ME, {})
    assert all("--method" not in c.args[0] or "GET" in c.args[0] for c in mock_cmd.call_args_list)
    return result


EMPTY_COMMITS = commits_json(EARLIER)


@pytest.mark.parametrize("present", [[], [0], [0, 1], [1, 2], [0, 2]])
def test_missing_any_of_three_not_passed(present):
    items = all_three()
    r = run([items[i] for i in present], [], [], EMPTY_COMMITS)
    assert r.status is GateStatus.NOT_PASSED


def test_zero_findings_passes():
    r = run(all_three(), [], [], EMPTY_COMMITS)
    assert r.status is GateStatus.PASSED
    assert r.reason == "zero-findings"


def test_regret_comment_ignored_for_t():
    regret = issue(f"x {REGRET_REVIEW_MARKER}", ts=LATER)
    r = run([*all_three(), regret, issue("later", AUTHOR, ts="2026-10-06T10:30:00Z")], [inline()], [], EMPTY_COMMITS)
    assert r.status is GateStatus.PASSED
    assert r.reason == "author-activity-after-T"


def test_inline_comments_count_for_t():
    # author comment is after the summary trio but before the newest inline comment
    r = run(
        [*all_three(), issue("reply", AUTHOR, ts="2026-10-06T10:30:00Z")],
        [inline(ts=LATER)],
        [],
        EMPTY_COMMITS,
    )
    assert r.status is GateStatus.NOT_PASSED


def test_author_issue_comment_passes():
    r = run([*all_three(), issue("ok", AUTHOR, ts=LATER)], [inline()], [], EMPTY_COMMITS)
    assert (r.status, r.reason) == (GateStatus.PASSED, "author-activity-after-T")


def test_author_inline_reply_passes():
    reply = issue("fixed", AUTHOR, ts=LATER)
    assert run(all_three(), [inline(), reply], [], EMPTY_COMMITS).status is GateStatus.PASSED


def test_author_review_uses_submitted_at():
    review = {"submitted_at": LATER, "state": "COMMENTED", "user": {"login": AUTHOR, "type": "User"}}
    assert run(all_three(), [inline()], [review], EMPTY_COMMITS).status is GateStatus.PASSED
    pending = {"submitted_at": None, "created_at": LATER, "user": {"login": AUTHOR, "type": "User"}}
    assert run(all_three(), [inline()], [pending], EMPTY_COMMITS).status is GateStatus.NOT_PASSED


def test_author_commit_after_t_passes():
    r = run(all_three(), [inline()], [], commits_json(LATER))
    assert (r.status, r.reason) == (GateStatus.PASSED, "author-activity-after-T")


def test_commit_by_other_author_ignored():
    assert run(all_three(), [inline()], [], commits_json(LATER, "bob")).status is GateStatus.NOT_PASSED


def test_commit_at_or_before_t_not_passed():
    assert run(all_three(), [inline()], [], commits_json(T)).status is GateStatus.NOT_PASSED
    assert run(all_three(), [inline()], [], commits_json(EARLIER)).status is GateStatus.NOT_PASSED


def test_equal_timestamp_not_passed():
    r = run([*all_three(), issue("same", AUTHOR, ts=T)], [inline()], [], EMPTY_COMMITS)
    assert r.status is GateStatus.NOT_PASSED


def test_acting_account_and_bot_activity_ignored():
    items = [
        *all_three(),
        issue("me", ME, ts=LATER),
        issue("bot", AUTHOR, ts=LATER, utype="Bot"),
        issue("bot2", "alice[bot]", ts=LATER),
        issue("other", "bob", ts=LATER),
    ]
    assert run(items, [inline()], [], EMPTY_COMMITS).status is GateStatus.NOT_PASSED


@pytest.mark.parametrize("failing", ["issues", "pulls", "reviews"])
def test_page_read_failure_is_read_failed(failing):
    def fake_pages(path, env):
        key = "issues" if "/issues/" in path else "reviews" if path.endswith("/reviews") else "pulls"
        return None if key == failing else []

    with (
        patch("harness.runners.auto_approve_github.fetch_all_pages", side_effect=fake_pages),
        patch("harness.runners.auto_approve_github.run_cmd") as mock_cmd,
    ):
        r = check_timing_gate("o/r", 7, AUTHOR, ME, {})
    assert r.status is GateStatus.READ_FAILED
    mock_cmd.assert_not_called()


def test_commits_read_failure_is_read_failed():
    assert run(all_three(), [inline()], [], None).status is GateStatus.READ_FAILED


def test_unknown_acting_account_is_read_failed():
    with patch("harness.runners.auto_approve_github.fetch_all_pages") as mock_pages:
        assert check_timing_gate("o/r", 7, AUTHOR, "", {}).status is GateStatus.READ_FAILED
    mock_pages.assert_not_called()
