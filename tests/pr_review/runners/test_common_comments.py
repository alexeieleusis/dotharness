import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dotharness.code_host import Capability, CodeHostError, CodeHostTimeoutError, UnsupportedCapabilityError
from dotharness.pr_review.runners.common import (
    DESIGN_REVIEW_MARKER,
    FOCUSED_REVIEW_MARKER,
    INLINE_REVIEW_MARKER,
    REGRET_REVIEW_MARKER,
    TRACEABILITY_REVIEW_MARKER,
    check_design_review_comment_status,
    check_pr_level_pass_comment_status,
    check_regret_review_comment_status,
    check_review_summary_comment_status,
    check_traceability_review_comment_status,
    fetch_pr_comments,
    fetch_review_threads,
    find_reply_with_marker,
    get_design_review_flagged_locations,
    get_pr_level_flagged_locations,
    get_traceability_review_flagged_locations,
    has_design_review_comment,
    has_inline_review_comments,
    has_pr_level_pass_comment,
    has_regret_review_comment,
    has_review_summary_comment,
    has_traceability_review_comment,
    merge_thread_state,
)
from tests.pr_review.runners.approve_support import NUMBER, REPO, T, make_host, put_comment, put_thread, seed_pr

ME = "alice"
OTHER = "someone-else"
MARKER = "<!-- some-future-pass-marker -->"


def _host(*, capabilities=None):
    host = make_host(**({"capabilities": capabilities} if capabilities is not None else {}))
    seed_pr(host)
    return host


# --- comment-status readers (Discussion.comments / Discussion.review_threads) -------------------------------------


async def test_has_review_summary_comment_matches_only_current_user():
    host = _host()
    put_comment(host, OTHER, "# Review Summary\nother account's comment", T)
    put_comment(host, ME, "unrelated comment", T)
    assert (await has_review_summary_comment(host.code_host(), REPO, NUMBER, ME)) is False
    put_comment(host, ME, "# Review Summary\nNo blocking issues found.", T)
    assert (await has_review_summary_comment(host.code_host(), REPO, NUMBER, ME)) is True
    assert {c.operation for c in host.calls} == {"Discussion.comments"}
    assert host.writes == []


async def test_comment_authored_by_nobody_never_matches():
    host = _host()
    put_comment(host, None, "# Review Summary", T)
    assert (await check_review_summary_comment_status(host.code_host(), REPO, NUMBER, ME)) is False


@pytest.mark.parametrize("operation", ["Discussion.comments", "Discussion.review_threads"])
async def test_failed_read_is_none_and_collapses_to_false_in_the_bool_form(operation):
    # The tri-state check lets callers tell "confirmed no comment" from "the host call itself failed".
    host = _host()
    host.fail_next(operation, CodeHostError("boom"))
    if operation == "Discussion.comments":
        assert (await check_review_summary_comment_status(host.code_host(), REPO, NUMBER, ME)) is None
        host.fail_next(operation, CodeHostError("boom"))
        assert (await has_review_summary_comment(host.code_host(), REPO, NUMBER, ME)) is False
    else:
        assert (await has_inline_review_comments(host.code_host(), REPO, NUMBER, ME)) is False


async def test_timeout_on_a_read_is_none_and_is_not_retried():
    host = _host()
    host.fail_next("Discussion.comments", CodeHostTimeoutError("slow", write=False))
    assert (await check_review_summary_comment_status(host.code_host(), REPO, NUMBER, ME)) is None
    assert [c.operation for c in host.calls].count("Discussion.comments") == 1


async def test_confirmed_absence_is_false_not_none():
    host = _host()
    assert (await check_review_summary_comment_status(host.code_host(), REPO, NUMBER, ME)) is False


@pytest.mark.parametrize("read", [check_review_summary_comment_status, has_inline_review_comments])
async def test_unsupported_capability_on_a_read_stops(read):
    host = _host(capabilities=frozenset(Capability) - {Capability.DISCUSSION})
    with pytest.raises(UnsupportedCapabilityError):
        await read(host.code_host(), REPO, NUMBER, ME)


async def test_has_inline_review_comments_reads_threads_and_matches_marker_and_user():
    host = _host()
    put_thread(host, [(OTHER, f"unrelated{INLINE_REVIEW_MARKER}", T)])
    put_thread(host, [(ME, "**S1234** Refactor this to reduce complexity.", T)])
    assert (await has_inline_review_comments(host.code_host(), REPO, NUMBER, ME)) is False
    put_thread(host, [(OTHER, "root", T), (ME, f"Nitpick: rename this.{INLINE_REVIEW_MARKER}", T)])
    assert (await has_inline_review_comments(host.code_host(), REPO, NUMBER, ME)) is True
    assert {c.operation for c in host.calls} == {"Discussion.review_threads"}


async def test_has_pr_level_pass_comment_uses_the_given_marker():
    host = _host()
    put_comment(host, OTHER, f"unrelated{MARKER}", T)
    put_comment(host, ME, "# Review Summary\nNo blocking issues found.", T)
    assert (await has_pr_level_pass_comment(host.code_host(), REPO, MARKER, NUMBER, ME)) is False
    put_comment(host, ME, f"# Some Pass\nfindings...{MARKER}", T)
    assert (await has_pr_level_pass_comment(host.code_host(), REPO, MARKER, NUMBER, ME)) is True
    assert (await check_pr_level_pass_comment_status(host.code_host(), REPO, MARKER, NUMBER, ME)) is True


@pytest.mark.parametrize(
    ("marker", "has", "check"),
    [
        (DESIGN_REVIEW_MARKER, has_design_review_comment, check_design_review_comment_status),
        (TRACEABILITY_REVIEW_MARKER, has_traceability_review_comment, check_traceability_review_comment_status),
        (REGRET_REVIEW_MARKER, has_regret_review_comment, check_regret_review_comment_status),
    ],
)
async def test_pass_comment_readers_match_marker_and_user(marker, has, check):
    host = _host()
    put_comment(host, OTHER, f"Review.{marker}", T)
    put_comment(host, ME, "# Review Summary\nNo blocking issues found.", T)
    assert (await has(host.code_host(), REPO, NUMBER, ME)) is False
    assert (await check(host.code_host(), REPO, NUMBER, ME)) is False
    put_comment(host, ME, f"Review.{marker}", T)
    assert (await has(host.code_host(), REPO, NUMBER, ME)) is True
    assert (await check(host.code_host(), REPO, NUMBER, ME)) is True


@pytest.mark.parametrize(
    ("check", "has"),
    [
        (check_design_review_comment_status, has_design_review_comment),
        (check_traceability_review_comment_status, has_traceability_review_comment),
        (check_regret_review_comment_status, has_regret_review_comment),
    ],
)
async def test_pass_comment_readers_distinguish_failed_read_from_absence(check, has):
    host = _host()
    host.fail_next("Discussion.comments", CodeHostError("boom"))
    assert (await check(host.code_host(), REPO, NUMBER, ME)) is None
    host.fail_next("Discussion.comments", CodeHostError("boom"))
    assert (await has(host.code_host(), REPO, NUMBER, ME)) is False


async def test_traceability_no_ticket_found_comment_counts_as_done():
    # Terminal outcome (requirement-traceability-requirements.md section 7.3): same marker as a completed comparison.
    host = _host()
    put_comment(
        host,
        ME,
        f"# Requirement Traceability\nNo linked ticket found - skipping scope/gap comparison.{TRACEABILITY_REVIEW_MARKER}",
        T,
    )
    assert (await has_traceability_review_comment(host.code_host(), REPO, NUMBER, ME)) is True


# --- flagged locations ---------------------------------------------------------------------------------------------


async def test_flagged_locations_are_anchors_of_the_users_marked_inline_comments():
    host = _host()
    put_thread(host, [(ME, f"flag{DESIGN_REVIEW_MARKER}", T)])
    put_thread(host, [(OTHER, f"flag{DESIGN_REVIEW_MARKER}", T)])
    put_thread(host, [(ME, "no marker", T)])
    put_thread(host, [(ME, f"flag{TRACEABILITY_REVIEW_MARKER}", T)])
    assert (await get_design_review_flagged_locations(host.code_host(), REPO, NUMBER, ME)) == [("a.py", 1)]
    assert (await get_traceability_review_flagged_locations(host.code_host(), REPO, NUMBER, ME)) == [("a.py", 1)]
    assert (await get_pr_level_flagged_locations(host.code_host(), REPO, MARKER, NUMBER, ME)) == []


async def test_flagged_locations_skip_threads_without_an_anchor():
    host = _host()
    thread = put_thread(host, [(ME, f"flag{DESIGN_REVIEW_MARKER}", T)])
    host._threads[REPO][NUMBER][0] = replace(thread, anchor=None)
    assert (await get_design_review_flagged_locations(host.code_host(), REPO, NUMBER, ME)) == []


async def test_flagged_locations_are_empty_on_a_failed_read():
    host = _host()
    put_thread(host, [(ME, f"flag{DESIGN_REVIEW_MARKER}", T)])
    host.fail_next("Discussion.review_threads", CodeHostError("boom"))
    assert (await get_design_review_flagged_locations(host.code_host(), REPO, NUMBER, ME)) == []


# --- review threads and thread-state merge -------------------------------------------------------------------------


async def test_fetch_review_threads_returns_every_thread():
    host = _host()
    first = put_thread(host, [(OTHER, "a", T)])
    second = put_thread(host, [(ME, "b", T)])
    threads = await fetch_review_threads(host.code_host(), REPO, NUMBER)
    assert threads == [first, second]


async def test_fetch_review_threads_is_none_on_failure_and_empty_list_when_there_are_none():
    host = _host()
    assert (await fetch_review_threads(host.code_host(), REPO, NUMBER)) == []
    host.fail_next("Discussion.review_threads", CodeHostError("boom"))
    assert (await fetch_review_threads(host.code_host(), REPO, NUMBER)) is None


async def test_fetch_review_threads_unsupported_capability_stops():
    host = _host(capabilities=frozenset(Capability) - {Capability.DISCUSSION})
    with pytest.raises(UnsupportedCapabilityError):
        await fetch_review_threads(host.code_host(), REPO, NUMBER)


def _root_id(thread):
    return thread.comments[0].id


def test_merge_thread_state_marks_resolved_and_minimized_root_comments():
    host = _host()
    thread = put_thread(host, [(OTHER, "root", T), (ME, "reply", T)])
    hidden = replace(thread.comments[0], is_hidden=True)
    thread = replace(thread, is_resolved=True, comments=(hidden, *thread.comments[1:]))
    comments = [{"type": "inline", "id": int(_root_id(thread))}]
    merge_thread_state(comments, [thread])
    assert comments[0]["thread_id"] == thread.id
    assert comments[0]["thread_resolved"] is True
    assert comments[0]["is_minimized"] is True
    assert "node_id" not in comments[0]


def test_merge_thread_state_unresolved_thread_sets_false_values():
    host = _host()
    thread = put_thread(host, [(OTHER, "root", T)])
    comments = [{"type": "inline", "id": int(_root_id(thread))}]
    merge_thread_state(comments, [thread])
    assert comments[0]["thread_resolved"] is False
    assert comments[0]["is_minimized"] is False


def test_merge_thread_state_matches_only_the_root_comment():
    host = _host()
    thread = put_thread(host, [(OTHER, "root", T), (ME, "reply", T)])
    comments = [{"type": "inline", "id": int(thread.comments[1].id)}]
    merge_thread_state(comments, [thread])
    assert "thread_id" not in comments[0]


def test_merge_thread_state_ignores_unknown_roots_and_non_inline_comments():
    host = _host()
    thread = put_thread(host, [(OTHER, "root", T)])
    other_id = int(_root_id(thread))
    comments = [
        {"type": "inline", "id": other_id + 1000},
        {"type": "review", "id": other_id},
        {"type": "issue", "id": other_id},
    ]
    expected = [dict(c) for c in comments]
    merge_thread_state(comments, [thread])
    assert comments == expected
    merge_thread_state(comments, [])
    assert comments == expected


async def test_fetch_pr_comments_returns_empty_on_script_failure(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        result = await fetch_pr_comments(1, tmp_path / "pr-comments.py", str(tmp_path), {})
    assert result == []


async def test_fetch_pr_comments_returns_empty_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        result = await fetch_pr_comments(1, tmp_path / "pr-comments.py", str(tmp_path), {})
    assert result == []


async def test_fetch_pr_comments_tags_types_and_filters_bots(tmp_path):
    data = {
        "inline_comments": [{"id": 1, "body": "x"}],
        "review_comments": [{"id": "review-2", "body": "y"}],
        "issue_comments": [
            {"id": 3, "author": "alice", "body": "human comment"},
            {"id": 4, "author": "some-bot[bot]", "body": "bot comment"},
            {"id": 5, "author": "alice", "body": "mentions bot] inline"},
        ],
    }

    def fake_run(cmd, **_kwargs):
        Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(data))
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch("dotharness.pr_review.runners.common.run_cmd", side_effect=fake_run):
        result = await fetch_pr_comments(9, tmp_path / "pr-comments.py", str(tmp_path), {})
    types = {c["id"]: c["type"] for c in result}
    assert types[1] == "inline"
    assert types["review-2"] == "review"
    assert types[3] == "issue"
    assert 4 not in types
    assert types[5] == "issue"


def test_find_reply_with_marker_returns_matching_reply():
    comment = {
        "replies": [
            {"author": "alice", "body": "unrelated"},
            {"author": "bot", "body": f"some suggestion\n\n{FOCUSED_REVIEW_MARKER}"},
        ]
    }
    reply = find_reply_with_marker(comment)
    assert reply is not None
    assert reply["author"] == "bot"


def test_find_reply_with_marker_returns_none_when_absent():
    comment = {"replies": [{"author": "alice", "body": "unrelated"}]}
    assert find_reply_with_marker(comment) is None


def test_find_reply_with_marker_handles_missing_replies_key():
    assert find_reply_with_marker({}) is None


def test_find_reply_with_marker_handles_none_replies():
    assert find_reply_with_marker({"replies": None}) is None
