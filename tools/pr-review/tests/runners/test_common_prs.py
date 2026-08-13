import json
from unittest.mock import MagicMock, patch

import pytest

from harness.runners.common import (
    add_reviewer,
    author_matches,
    get_current_user,
    get_requested_reviewers,
    has_inline_review_comments,
    has_review_summary_comment,
    is_inline_review_comment,
    is_pr_open,
    is_review_summary_comment,
    list_open_prs_for_current_user,
    list_open_prs_matching_authors,
    pr_from_url,
)


def test_author_matches_star_allows_all():
    assert author_matches("anyone", "*") is True


def test_author_matches_list_rejects_unknown():
    assert author_matches("eve", ["alice", "bob"]) is False


def test_author_matches_list_allows_known():
    assert author_matches("alice", ["alice", "bob"]) is True


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, b"OPEN\n", True),
        (0, b"CLOSED\n", False),
        (0, b"MERGED\n", False),
        (1, b"", False),  # fails closed when gh lookup fails
    ],
)
def test_is_pr_open(returncode, stdout, expected):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=returncode, stdout=stdout, stderr=b"")
        assert is_pr_open(8809, "acme/repo", {}) is expected


def test_list_open_prs_filters_drafts_and_authors(tmp_path):
    payload = [
        {"number": 3, "headRefName": "c", "author": {"login": "alice"}, "isDraft": False},
        {"number": 1, "headRefName": "a", "author": {"login": "alice"}, "isDraft": True},
        {"number": 2, "headRefName": "b", "author": {"login": "eve"}, "isDraft": False},
    ]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload).encode())
        result = list_open_prs_matching_authors("acme/repo", ["alice"], str(tmp_path), {})
    assert result is not None
    assert [p["number"] for p in result] == [3]


def test_list_open_prs_sorted_ascending():
    payload = [
        {"number": 9, "headRefName": "b", "author": {"login": "alice"}, "isDraft": False},
        {"number": 2, "headRefName": "a", "author": {"login": "alice"}, "isDraft": False},
    ]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload).encode())
        result = list_open_prs_matching_authors("acme/repo", "*", "/", {})
    assert result is not None
    assert [p["number"] for p in result] == [2, 9]


def test_list_open_prs_returns_none_on_gh_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        result = list_open_prs_matching_authors("acme/repo", "*", "/", {})
    assert result is None


def _run_cmd_router(responses):
    """Build a run_cmd side_effect that dispatches on argv shape, for tests exercising
    list_open_prs_for_current_user (which issues several distinct gh invocations)."""

    def _router(cmd, **_kwargs):
        if "--author" in cmd:
            return responses["author"]
        if "--assignee" in cmd:
            return responses["assignee"]
        if "search" in cmd:
            return responses["search"]
        if "view" in cmd:
            number = int(cmd[cmd.index("view") + 1])
            return responses["view"][number]
        msg = f"unexpected cmd: {cmd}"
        raise AssertionError(msg)

    return _router


def test_list_open_prs_for_current_user_unions_author_assignee_review_requested():
    author_payload = [{"number": 1, "headRefName": "a", "isDraft": False}]
    assignee_payload = [{"number": 2, "headRefName": "b", "isDraft": False}]
    search_payload = [{"number": 3}]
    view_payload = {3: {"number": 3, "headRefName": "c", "isDraft": False}}
    responses = {
        "author": MagicMock(returncode=0, stdout=json.dumps(author_payload).encode()),
        "assignee": MagicMock(returncode=0, stdout=json.dumps(assignee_payload).encode()),
        "search": MagicMock(returncode=0, stdout=json.dumps(search_payload).encode()),
        "view": {3: MagicMock(returncode=0, stdout=json.dumps(view_payload[3]).encode())},
    }
    with patch("harness.runners.common.run_cmd", side_effect=_run_cmd_router(responses)):
        result = list_open_prs_for_current_user("acme/repo", "/", {})
    assert [p["number"] for p in result] == [1, 2, 3]


def test_list_open_prs_for_current_user_dedupes_overlapping_sources():
    shared = {"number": 1, "headRefName": "a", "isDraft": False}
    search_payload = [{"number": 1}]
    responses = {
        "author": MagicMock(returncode=0, stdout=json.dumps([shared]).encode()),
        "assignee": MagicMock(returncode=0, stdout=json.dumps([shared]).encode()),
        "search": MagicMock(returncode=0, stdout=json.dumps(search_payload).encode()),
        "view": {1: MagicMock(returncode=0, stdout=json.dumps(shared).encode())},
    }
    with patch("harness.runners.common.run_cmd", side_effect=_run_cmd_router(responses)):
        result = list_open_prs_for_current_user("acme/repo", "/", {})
    assert [p["number"] for p in result] == [1]


def test_list_open_prs_for_current_user_filters_drafts():
    author_payload = [
        {"number": 1, "headRefName": "a", "isDraft": True},
        {"number": 2, "headRefName": "b", "isDraft": False},
    ]
    responses = {
        "author": MagicMock(returncode=0, stdout=json.dumps(author_payload).encode()),
        "assignee": MagicMock(returncode=0, stdout=json.dumps([]).encode()),
        "search": MagicMock(returncode=0, stdout=json.dumps([]).encode()),
        "view": {},
    }
    with patch("harness.runners.common.run_cmd", side_effect=_run_cmd_router(responses)):
        result = list_open_prs_for_current_user("acme/repo", "/", {})
    assert [p["number"] for p in result] == [2]


def test_list_open_prs_for_current_user_handles_gh_failures():
    responses = {
        "author": MagicMock(returncode=1, stdout=b"", stderr=b"boom"),
        "assignee": MagicMock(returncode=1, stdout=b"", stderr=b"boom"),
        "search": MagicMock(returncode=1, stdout=b"", stderr=b"boom"),
        "view": {},
    }
    with patch("harness.runners.common.run_cmd", side_effect=_run_cmd_router(responses)):
        result = list_open_prs_for_current_user("acme/repo", "/", {})
    assert result == []


def test_get_current_user_returns_login():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"alice\n")
        assert get_current_user({}) == "alice"


@pytest.mark.parametrize(
    ("review_requests", "expected"),
    [
        ([{"login": "alice"}, {"login": "bob"}], ["alice", "bob"]),
        ([{"login": "alice"}, {"slug": "some-team"}], ["alice"]),
    ],
)
def test_get_requested_reviewers_returns_logins(review_requests, expected):
    payload = {"reviewRequests": review_requests}
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload).encode())
        assert get_requested_reviewers(1, "acme/repo", {}) == expected


def test_get_requested_reviewers_returns_empty_on_gh_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        assert get_requested_reviewers(1, "acme/repo", {}) == []


def test_pr_from_url_parses_number_and_fetches_pr():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b'{"number": 8484, "headRefName": "feat", "baseRefName": "main"}',
        )
        pr = pr_from_url(
            "https://github.com/acme/frontend/pull/8484/", "acme/frontend", {}, "number,headRefName,baseRefName"
        )
    assert pr == {"number": 8484, "headRefName": "feat", "baseRefName": "main"}
    args = mock_run.call_args.args[0]
    assert args[:3] == ["gh", "pr", "view"]
    assert "8484" in args


def test_pr_from_url_returns_empty_on_gh_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"auth failed")
        result = pr_from_url("https://github.com/acme/repo/pull/999", "acme/repo", {}, "number")
    assert result == {}


def test_add_reviewer_invokes_gh_pr_edit():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        add_reviewer(1, "acme/repo", "alice", {})
    cmd = mock_run.call_args.args[0]
    assert cmd == ["gh", "pr", "edit", "1", "--repo", "acme/repo", "--add-reviewer", "alice"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Review Summary\nNo blocking issues found.", True),
        ("# review summary\nlowercase heading still counts", True),
        ("## REVIEW SUMMARY", True),
        ("Review summary without a leading hash", True),
        ("# Some title\n\nReview Summary: no issues", True),
        ("osc-review passed", True),
        ("# Just a heading about something else", False),
        ("", False),
        ("   \n\n  ", False),
    ],
)
def test_is_review_summary_comment(body, expected):
    assert is_review_summary_comment(body) is expected


def test_has_review_summary_comment_matches_only_current_user():
    comments = [
        {"user": {"login": "someone-else"}, "body": "# Review Summary\nother account's comment"},
        {"user": {"login": "alice"}, "body": "unrelated comment"},
        {"user": {"login": "alice"}, "body": "# Review Summary\nNo blocking issues found."},
    ]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(comments).encode())
        assert has_review_summary_comment(1, "acme/repo", "alice", {}) is True


def test_has_review_summary_comment_false_when_only_other_user_posted():
    comments = [{"user": {"login": "someone-else"}, "body": "# Review Summary\nnot alice's comment"}]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(comments).encode())
        assert has_review_summary_comment(1, "acme/repo", "alice", {}) is False


def test_has_review_summary_comment_returns_false_on_gh_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        assert has_review_summary_comment(1, "acme/repo", "alice", {}) is False


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Nitpick: rename this variable.<!-- osc-review-inline -->", True),
        ("<!-- osc-review-inline -->", True),
        ("Nitpick: rename this variable.", False),
        ("**S1234** Refactor this to reduce complexity.", False),
        ("", False),
    ],
)
def test_is_inline_review_comment(body, expected):
    assert is_inline_review_comment(body) is expected


def test_has_inline_review_comments_true_when_marker_and_user_match():
    comments = [
        {"user": {"login": "someone-else"}, "body": "unrelated<!-- osc-review-inline -->"},
        {"user": {"login": "alice"}, "body": "Nitpick: rename this.<!-- osc-review-inline -->"},
    ]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(comments).encode())
        assert has_inline_review_comments(1, "acme/repo", "alice", {}) is True


def test_has_inline_review_comments_false_when_same_user_but_no_marker():
    # Regression test: a vibe-heal/SonarQube review comment posted under the same GitHub
    # account must not be mistaken for a partial review_requested run.
    comments = [{"user": {"login": "alice"}, "body": "**S1234** Refactor this to reduce complexity."}]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(comments).encode())
        assert has_inline_review_comments(1, "acme/repo", "alice", {}) is False


def test_has_inline_review_comments_false_when_other_user_posted_with_marker():
    comments = [{"user": {"login": "someone-else"}, "body": "Nitpick.<!-- osc-review-inline -->"}]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(comments).encode())
        assert has_inline_review_comments(1, "acme/repo", "alice", {}) is False


def test_has_inline_review_comments_returns_false_on_gh_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        assert has_inline_review_comments(1, "acme/repo", "alice", {}) is False
