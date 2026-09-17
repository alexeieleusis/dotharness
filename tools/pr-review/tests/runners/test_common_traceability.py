import json
from unittest.mock import MagicMock, patch

from harness.runners.common import build_early_comment_context, resolve_linked_tickets

REPO = "acme/repo"


def _comment(body: str, created_at: str, comment_id: int = 1) -> dict:
    return {"id": comment_id, "body": body, "created_at": created_at}


def _issue(number: int, title: str = "Ticket title", body: str = "Ticket body") -> dict:
    return {"number": number, "title": title, "body": body}


def _router(comments_by_path_and_page: dict, issues_by_repo_and_number: dict):
    """Build a run_cmd side_effect dispatching `gh api ... issues/{pr}/comments` paginated
    fetches and `gh issue view` lookups, mirroring test_common_prs.py's _run_cmd_router."""

    def _dispatch(cmd, **_kwargs):
        if cmd[:2] == ["gh", "api"]:
            path = cmd[4]
            page = int(cmd[-1].split("=")[1])
            payload = comments_by_path_and_page.get((path, page), [])
            return MagicMock(returncode=0, stdout=json.dumps(payload).encode())
        if cmd[:2] == ["gh", "issue"]:
            number = int(cmd[3])
            repo = cmd[cmd.index("--repo") + 1]
            entry = issues_by_repo_and_number.get((repo, number))
            if entry is None:
                return MagicMock(
                    returncode=1,
                    stdout=b"",
                    stderr=f"GraphQL: Could not resolve to an issue or pull request with the number of {number}.".encode(),
                )
            return MagicMock(returncode=0, stdout=json.dumps(entry).encode())
        msg = f"unexpected cmd: {cmd}"
        raise AssertionError(msg)

    return _dispatch


# --- resolve_linked_tickets: native linking (closingIssuesReferences) -----------------


def test_resolve_linked_tickets_native_link_single_ticket():
    pr = {
        "number": 39,
        "createdAt": "2026-01-01T00:00:00Z",
        "closingIssuesReferences": [
            {
                "number": 3,
                "url": "https://github.com/acme/repo/issues/3",
                "repository": {"name": "repo", "owner": {"login": "acme"}},
            }
        ],
    }
    router = _router({}, {(REPO, 3): _issue(3, "Do the thing", "Body of 3")})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == [
        {"number": 3, "repo": REPO, "title": "Do the thing", "body": "Body of 3", "source": "closing_keyword"}
    ]


def test_resolve_linked_tickets_native_link_uses_entrys_own_repo():
    pr = {
        "number": 39,
        "createdAt": "2026-01-01T00:00:00Z",
        "closingIssuesReferences": [
            {"number": 7, "url": "...", "repository": {"name": "other", "owner": {"login": "someone-else"}}}
        ],
    }
    router = _router({}, {("someone-else/other", 7): _issue(7)})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == [
        {
            "number": 7,
            "repo": "someone-else/other",
            "title": "Ticket title",
            "body": "Ticket body",
            "source": "closing_keyword",
        }
    ]


def test_resolve_linked_tickets_native_link_dedupes_by_repo_and_number():
    pr = {
        "number": 39,
        "createdAt": "2026-01-01T00:00:00Z",
        "closingIssuesReferences": [
            {"number": 3, "repository": {"name": "repo", "owner": {"login": "acme"}}},
            {"number": 3, "repository": {"name": "repo", "owner": {"login": "acme"}}},
        ],
    }
    router = _router({}, {(REPO, 3): _issue(3)})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets is not None
    assert len(tickets) == 1


def test_resolve_linked_tickets_native_link_drops_unresolvable_entries():
    pr = {
        "number": 39,
        "createdAt": "2026-01-01T00:00:00Z",
        "closingIssuesReferences": [
            {"number": 404, "repository": {"name": "repo", "owner": {"login": "acme"}}},
            {"number": 3, "repository": {"name": "repo", "owner": {"login": "acme"}}},
        ],
    }
    router = _router({}, {(REPO, 3): _issue(3)})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets is not None
    assert [t["number"] for t in tickets] == [3]


def test_resolve_linked_tickets_native_link_skips_malformed_entries():
    pr = {
        "number": 39,
        "createdAt": "2026-01-01T00:00:00Z",
        "closingIssuesReferences": [{"number": 3, "repository": {}}],
    }
    # The malformed entry is dropped, native linking yields nothing, and resolution falls
    # through to the (here, also empty) comment-scan fallback.
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): []}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == []


def test_resolve_linked_tickets_native_link_returns_none_when_ref_lookup_is_inconclusive():
    """A closing-keyword ref that fails to resolve for a reason other than confirmed
    absence (rate limit, transient 5xx) must surface as None, not as "no ticket" — and the
    comment-scan fallback must not even run, since it would very likely also come up empty
    and let a real linked ticket get lost behind the terminal "no ticket" comment."""
    pr = {
        "number": 39,
        "createdAt": "2026-01-01T00:00:00Z",
        "closingIssuesReferences": [{"number": 3, "repository": {"name": "repo", "owner": {"login": "acme"}}}],
    }

    def _dispatch(cmd, **_kwargs):
        assert cmd[:2] == ["gh", "issue"], f"comment-scan fallback must not run, got: {cmd}"
        return MagicMock(returncode=1, stdout=b"", stderr=b"API rate limit exceeded")

    with patch("harness.runners.common.run_cmd", side_effect=_dispatch):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets is None


def test_resolve_linked_tickets_native_link_confirmed_absent_falls_back_to_comment_scan():
    """Unlike an inconclusive failure, a confirmed-absent ref (404, or a number that names
    a pull request rather than an issue) is treated the same as "no native link" and does
    fall through to the comment-scan fallback."""
    pr = {
        "number": 39,
        "createdAt": "2026-01-01T00:00:00Z",
        "closingIssuesReferences": [{"number": 404, "repository": {"name": "repo", "owner": {"login": "acme"}}}],
    }
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): []}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == []


# --- resolve_linked_tickets: comment-scan fallback -------------------------------------


def test_resolve_linked_tickets_falls_back_to_comment_scan_when_no_native_links():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    comments = [_comment("Related ticket: #3", "2026-01-01T00:01:00Z")]
    router = _router(
        {(f"repos/{REPO}/issues/39/comments", 1): comments},
        {(REPO, 3): _issue(3, "Do the thing")},
    )
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == [{"number": 3, "repo": REPO, "title": "Do the thing", "body": "Ticket body", "source": "comment"}]


def test_resolve_linked_tickets_comment_scan_full_url_reference():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    comments = [_comment("See https://github.com/acme/repo/issues/3 for context", "2026-01-01T00:01:00Z")]
    router = _router(
        {(f"repos/{REPO}/issues/39/comments", 1): comments},
        {(REPO, 3): _issue(3)},
    )
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets is not None
    assert [t["number"] for t in tickets] == [3]
    assert tickets[0]["source"] == "comment"


def test_resolve_linked_tickets_comment_scan_cross_repo_shorthand():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    comments = [_comment("Tracked in someorg/othereproj#42", "2026-01-01T00:01:00Z")]
    router = _router(
        {(f"repos/{REPO}/issues/39/comments", 1): comments},
        {("someorg/othereproj", 42): _issue(42)},
    )
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets is not None
    assert tickets[0]["repo"] == "someorg/othereproj"
    assert tickets[0]["number"] == 42


def test_resolve_linked_tickets_comment_scan_drops_reference_to_a_pull_request():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    comments = [_comment("Superseded by #40", "2026-01-01T00:01:00Z")]
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): comments}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == []


def test_resolve_linked_tickets_comment_scan_includes_bot_authored_comments():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    comments = [
        {"id": 1, "body": "Linked: #3", "created_at": "2026-01-01T00:01:00Z", "user": {"login": "jira-bridge[bot]"}}
    ]
    router = _router(
        {(f"repos/{REPO}/issues/39/comments", 1): comments},
        {(REPO, 3): _issue(3)},
    )
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets is not None
    assert [t["number"] for t in tickets] == [3]


def test_resolve_linked_tickets_comment_scan_excludes_comments_outside_window():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    comments = [_comment("Too late: #3", "2026-01-01T00:05:01Z")]
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): comments}, {(REPO, 3): _issue(3)})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == []


def test_resolve_linked_tickets_comment_scan_dedupes_repeated_references():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    comments = [
        _comment("See #3", "2026-01-01T00:01:00Z", comment_id=1),
        _comment("Also #3", "2026-01-01T00:02:00Z", comment_id=2),
    ]
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): comments}, {(REPO, 3): _issue(3)})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets is not None
    assert len(tickets) == 1


def test_resolve_linked_tickets_returns_empty_when_nothing_resolves():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): []}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == []


def test_resolve_linked_tickets_returns_empty_when_comment_fetch_fails():
    pr = {"number": 39, "createdAt": "2026-01-01T00:00:00Z", "closingIssuesReferences": []}
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        tickets = resolve_linked_tickets(pr, REPO, {})
    assert tickets == []


# --- build_early_comment_context --------------------------------------------------------


def test_build_early_comment_context_concatenates_bodies_within_window():
    comments = [
        _comment("first note", "2026-01-01T00:01:00Z", comment_id=1),
        _comment("second note", "2026-01-01T00:04:00Z", comment_id=2),
    ]
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): comments}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        context = build_early_comment_context(39, REPO, "2026-01-01T00:00:00Z", {})
    assert context == "first note\n\nsecond note"


def test_build_early_comment_context_excludes_comments_after_window():
    comments = [
        _comment("in window", "2026-01-01T00:05:00Z", comment_id=1),
        _comment("out of window", "2026-01-01T00:05:01Z", comment_id=2),
    ]
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): comments}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        context = build_early_comment_context(39, REPO, "2026-01-01T00:00:00Z", {})
    assert context == "in window"


def test_build_early_comment_context_returns_empty_string_when_no_comments():
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): []}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        context = build_early_comment_context(39, REPO, "2026-01-01T00:00:00Z", {})
    assert context == ""


def test_build_early_comment_context_returns_empty_string_on_fetch_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        context = build_early_comment_context(39, REPO, "2026-01-01T00:00:00Z", {})
    assert context == ""


def test_build_early_comment_context_returns_empty_string_on_unparseable_created_at():
    router = _router({(f"repos/{REPO}/issues/39/comments", 1): [_comment("note", "2026-01-01T00:01:00Z")]}, {})
    with patch("harness.runners.common.run_cmd", side_effect=router):
        context = build_early_comment_context(39, REPO, "not-a-timestamp", {})
    assert context == ""


def test_build_early_comment_context_paginates():
    page_one = [_comment(f"note {i}", "2026-01-01T00:01:00Z", comment_id=i) for i in range(100)]
    page_two = [_comment("note 100", "2026-01-01T00:02:00Z", comment_id=100)]
    router = _router(
        {
            (f"repos/{REPO}/issues/39/comments", 1): page_one,
            (f"repos/{REPO}/issues/39/comments", 2): page_two,
        },
        {},
    )
    with patch("harness.runners.common.run_cmd", side_effect=router):
        context = build_early_comment_context(39, REPO, "2026-01-01T00:00:00Z", {})
    assert context.count("note") == 101
