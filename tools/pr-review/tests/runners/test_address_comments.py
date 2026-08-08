import json
from unittest.mock import ANY, MagicMock, patch

import pytest

from harness.runners import address_comments
from harness.runners.address_comments import (
    _address_single_comment,
    _focused_review_approved,
    _split_gated_focused_review_comments,
)
from harness.runners.common import FOCUSED_REVIEW_MARKER


def _cfg(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    return HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        vibe_heal=VibehealConfig(),
    )


def _setup_knowledge(tmp_path, filename, content="instructions"):
    d = tmp_path / "k" / "pr-review"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(content)


def test_skips_pr_with_no_pending_feedback(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path, "address-comment.md")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.address_comments.get_gh_token", return_value="tok"),
        patch("harness.runners.address_comments._list_prs_to_check", return_value=[{"number": 1, "headRefName": "b"}]),
        patch("harness.runners.address_comments._has_pending_feedback", return_value=False),
        patch("harness.runners.address_comments.git_detach_and_record", return_value="sha"),
        patch("harness.runners.address_comments.Backend") as mock_be,
    ):
        address_comments._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()


_FAKE_COMMENT = {
    "type": "inline",
    "id": 42,
    "author": "alice",
    "path": "a.py",
    "line": 1,
    "body": "fix this",
    "url": "http://x",
    "diff_hunk": "",
    "replies": [],
}


def test_runs_backend_when_unresolved_threads(tmp_xdg, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "k" / "pr-review").mkdir(parents=True)
    (tmp_path / "k" / "pr-review" / "address-comment.md").write_text("instructions")
    with (
        patch("harness.runners.address_comments.get_gh_token", return_value="tok"),
        patch("harness.runners.address_comments._list_prs_to_check", return_value=[{"number": 1, "headRefName": "b"}]),
        patch("harness.runners.address_comments._has_pending_feedback", return_value=True),
        patch("harness.runners.address_comments.fetch_pr_comments", return_value=[_FAKE_COMMENT]),
        patch("harness.runners.address_comments.git_detach_and_record", return_value="sha"),
        patch("harness.runners.address_comments.git_fetch_and_checkout"),
        patch("harness.runners.address_comments.git_restore"),
        patch("harness.runners.address_comments.run_cmd") as mock_run,
        patch("harness.runners.common.run_cmd", mock_run),
        patch("harness.runners.address_comments.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        address_comments._run_locked(cfg)
    mock_be.return_value.run.assert_called_once()


def test_pushes_branch_after_backend(tmp_xdg, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "k" / "pr-review").mkdir(parents=True)
    (tmp_path / "k" / "pr-review" / "address-comment.md").write_text("instructions")
    with (
        patch("harness.runners.address_comments.get_gh_token", return_value="tok"),
        patch(
            "harness.runners.address_comments._list_prs_to_check",
            return_value=[{"number": 2, "headRefName": "my-branch"}],
        ),
        patch("harness.runners.address_comments._has_pending_feedback", return_value=True),
        patch("harness.runners.address_comments.fetch_pr_comments", return_value=[_FAKE_COMMENT]),
        patch("harness.runners.address_comments.git_detach_and_record", return_value="sha"),
        patch("harness.runners.address_comments.git_fetch_and_checkout"),
        patch("harness.runners.address_comments.git_restore"),
        patch("harness.runners.address_comments.run_cmd") as mock_run,
        patch("harness.runners.common.run_cmd", mock_run),
        patch("harness.runners.address_comments.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        address_comments._run_locked(cfg)
    push_calls = [c for c in mock_run.call_args_list if "push" in str(c)]
    assert any("my-branch" in str(c) for c in push_calls)


def test_pushes_after_each_comment_not_once_at_end(tmp_xdg, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "k" / "pr-review").mkdir(parents=True)
    (tmp_path / "k" / "pr-review" / "address-comment.md").write_text("instructions")
    comment_2 = {**_FAKE_COMMENT, "id": 43}
    with (
        patch("harness.runners.address_comments.get_gh_token", return_value="tok"),
        patch(
            "harness.runners.address_comments._list_prs_to_check",
            return_value=[{"number": 2, "headRefName": "my-branch"}],
        ),
        patch("harness.runners.address_comments._has_pending_feedback", return_value=True),
        patch("harness.runners.address_comments.fetch_pr_comments", return_value=[_FAKE_COMMENT, comment_2]),
        patch("harness.runners.address_comments.git_detach_and_record", return_value="sha"),
        patch("harness.runners.address_comments.git_fetch_and_checkout"),
        patch("harness.runners.address_comments.git_restore"),
        patch("harness.runners.address_comments.run_cmd") as mock_run,
        patch("harness.runners.common.run_cmd", mock_run),
        patch("harness.runners.address_comments.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        address_comments._run_locked(cfg)
    push_calls = [c for c in mock_run.call_args_list if c.args and "push" in c.args[0]]
    assert len(push_calls) == 2  # one push per comment, not a single push after the loop
    assert mock_be.return_value.run.call_count == 2  # both comments' backends ran


def test_stops_processing_comments_after_push_failure(tmp_xdg, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "k" / "pr-review").mkdir(parents=True)
    (tmp_path / "k" / "pr-review" / "address-comment.md").write_text("instructions")
    comment_2 = {**_FAKE_COMMENT, "id": 43}

    def run_cmd_side_effect(cmd, **kwargs):
        if "push" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"! [rejected]")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch("harness.runners.address_comments.get_gh_token", return_value="tok"),
        patch(
            "harness.runners.address_comments._list_prs_to_check",
            return_value=[{"number": 2, "headRefName": "my-branch"}],
        ),
        patch("harness.runners.address_comments._has_pending_feedback", return_value=True),
        patch("harness.runners.address_comments.fetch_pr_comments", return_value=[_FAKE_COMMENT, comment_2]),
        patch("harness.runners.address_comments.git_detach_and_record", return_value="sha"),
        patch("harness.runners.address_comments.git_fetch_and_checkout"),
        patch("harness.runners.address_comments.git_restore") as mock_restore,
        patch("harness.runners.address_comments.run_cmd", side_effect=run_cmd_side_effect),
        patch("harness.runners.common.run_cmd", side_effect=run_cmd_side_effect),
        patch("harness.runners.address_comments.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        address_comments._run_locked(cfg)
    # the first comment's backend ran and its push was attempted (and failed); the second
    # comment's backend must never run, since its predecessor's push already failed
    assert mock_be.return_value.run.call_count == 1
    mock_restore.assert_called()


def test_stops_processing_comments_after_history_rewrite(tmp_xdg, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "k" / "pr-review").mkdir(parents=True)
    (tmp_path / "k" / "pr-review" / "address-comment.md").write_text("instructions")
    comment_2 = {**_FAKE_COMMENT, "id": 43}
    rev_parse_calls = {"count": 0}

    def run_cmd_side_effect(cmd, **kwargs):
        if "rev-parse" in cmd:
            rev_parse_calls["count"] += 1
            # first rev-parse is the pre-loop head_sha; the one inside the (only)
            # comment's backend run reports a HEAD that moved
            sha = "sha-before" if rev_parse_calls["count"] == 1 else "sha-after"
            return MagicMock(returncode=0, stdout=f"{sha}\n".encode(), stderr=b"")
        if "merge-base" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"")  # not an ancestor: history was rewritten
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    mock_run = MagicMock(side_effect=run_cmd_side_effect)
    with (
        patch("harness.runners.address_comments.get_gh_token", return_value="tok"),
        patch(
            "harness.runners.address_comments._list_prs_to_check",
            return_value=[{"number": 2, "headRefName": "my-branch"}],
        ),
        patch("harness.runners.address_comments._has_pending_feedback", return_value=True),
        patch("harness.runners.address_comments.fetch_pr_comments", return_value=[_FAKE_COMMENT, comment_2]),
        patch("harness.runners.address_comments.git_detach_and_record", return_value="sha"),
        patch("harness.runners.address_comments.git_fetch_and_checkout"),
        patch("harness.runners.address_comments.git_restore") as mock_restore,
        patch("harness.runners.address_comments.run_cmd", mock_run),
        patch("harness.runners.common.run_cmd", mock_run),
        patch("harness.runners.address_comments.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        address_comments._run_locked(cfg)
    # the first comment's backend ran; the fast-forward check must have caught the
    # rewrite and skipped both the push and the second comment entirely
    assert mock_be.return_value.run.call_count == 1
    push_calls = [c for c in mock_run.call_args_list if c.args and "push" in c.args[0]]
    assert push_calls == []
    mock_restore.assert_called()


def test_list_prs_to_check_merges_author_and_assignee_deduped():
    author_payload = [{"number": 1, "headRefName": "a", "isDraft": False}]
    assignee_payload = [
        {"number": 1, "headRefName": "a", "isDraft": False},
        {"number": 2, "headRefName": "b", "isDraft": False},
    ]
    responses = [
        MagicMock(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")
        for payload in (author_payload, assignee_payload)
    ]

    with patch("harness.runners.address_comments.run_cmd", side_effect=responses):
        prs = address_comments._list_prs_to_check("acme/frontend", {})

    assert [p["number"] for p in prs] == [1, 2]


def test_restores_head_even_on_backend_failure(tmp_xdg, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "k" / "pr-review").mkdir(parents=True)
    (tmp_path / "k" / "pr-review" / "address-comment.md").write_text("instructions")
    with (
        patch("harness.runners.address_comments.get_gh_token", return_value="tok"),
        patch("harness.runners.address_comments._list_prs_to_check", return_value=[{"number": 3, "headRefName": "b"}]),
        patch("harness.runners.address_comments._has_pending_feedback", return_value=True),
        patch("harness.runners.address_comments.fetch_pr_comments", return_value=[_FAKE_COMMENT]),
        patch("harness.runners.address_comments.git_detach_and_record", return_value="sha"),
        patch("harness.runners.address_comments.git_fetch_and_checkout"),
        patch("harness.runners.address_comments.git_restore") as mock_restore,
        patch("harness.runners.address_comments.run_cmd"),
        patch("harness.runners.common.run_cmd"),
        patch("harness.runners.address_comments.Backend") as mock_be,
    ):
        mock_be.return_value.run.side_effect = Exception("backend exploded")
        address_comments._run_locked(cfg)
    mock_restore.assert_called()


@pytest.mark.parametrize(
    "post_sha_stdout, is_ancestor_returncode, expect_error",
    [
        (b"sha-after\n", 1, True),  # HEAD moved and is-ancestor says no: history was rewritten
        (b"sha-before\n", 0, False),  # HEAD unchanged: is-ancestor check is skipped entirely
        (b"sha-after\n", 0, False),  # HEAD moved but is-ancestor says yes: clean fast-forward
    ],
)
def test_address_single_comment_history_rewrite_detection(
    post_sha_stdout, is_ancestor_returncode, expect_error, caplog
):
    responses = iter([
        MagicMock(returncode=0, stdout=post_sha_stdout, stderr=b""),  # post_sha rev-parse
        MagicMock(returncode=is_ancestor_returncode, stdout=b"", stderr=b""),  # merge-base --is-ancestor
    ])
    mock_backend = MagicMock()
    mock_backend.run.return_value = MagicMock(returncode=0)
    run_cmd_mock = MagicMock(side_effect=lambda *a, **k: next(responses))
    with (
        patch("harness.runners.address_comments.run_cmd", run_cmd_mock),
        patch("harness.runners.common.run_cmd", run_cmd_mock),
        caplog.at_level("ERROR"),
    ):
        post_sha, rewritten = _address_single_comment(
            _FAKE_COMMENT, 1, "instructions", mock_backend, "/repo", "acme/frontend", {}, "sha-before"
        )
    assert any("not a fast-forward" in r.message for r in caplog.records) == expect_error
    assert post_sha == post_sha_stdout.decode().strip()
    assert rewritten == expect_error


_MARKED_COMMENT = {
    "type": "inline",
    "id": 1,
    "author": "sonar",
    "path": "a.py",
    "line": 1,
    "body": "terse sonar message",
    "url": "http://x",
    "diff_hunk": "",
    "replies": [{"id": 99, "author": "alexei", "body": f"suggested fix\n\n{FOCUSED_REVIEW_MARKER}"}],
}

_PLAIN_COMMENT = {
    "type": "inline",
    "id": 2,
    "author": "alice",
    "path": "b.py",
    "line": 1,
    "body": "please fix",
    "url": "http://y",
    "diff_hunk": "",
    "replies": [],
}

_ADDRESSED_MARKED_COMMENT = {
    "type": "inline",
    "id": 1,
    "author": "sonar",
    "path": "a.py",
    "line": 1,
    "body": "terse sonar message",
    "url": "http://x",
    "diff_hunk": "",
    "replies": [
        {"id": 99, "author": "alexei", "body": f"suggested fix\n\n{FOCUSED_REVIEW_MARKER}"},
        {"id": 100, "author": "alexei", "body": "Addressed in http://commit-url"},
    ],
}


def test_split_gated_focused_review_comments_disabled_returns_all_ungated():
    gated, ungated = _split_gated_focused_review_comments([_MARKED_COMMENT, _PLAIN_COMMENT], enabled=False)
    assert gated == []
    assert ungated == [_MARKED_COMMENT, _PLAIN_COMMENT]


def test_split_gated_focused_review_comments_enabled_splits_marked_from_plain():
    gated, ungated = _split_gated_focused_review_comments([_MARKED_COMMENT, _PLAIN_COMMENT], enabled=True)
    assert gated == [_MARKED_COMMENT]
    assert ungated == [_PLAIN_COMMENT]


def test_split_gated_focused_review_comments_ignores_non_inline_types():
    review_comment = {"type": "review", "id": "review-1", "body": FOCUSED_REVIEW_MARKER, "replies": []}
    gated, ungated = _split_gated_focused_review_comments([review_comment], enabled=True)
    assert gated == []
    assert ungated == [review_comment]


def test_split_gated_focused_review_comments_excludes_already_addressed_thread():
    # The marker reply is no longer the LAST reply (our own completion reply is), so
    # this thread must NOT be re-gated — it should fall through to normal filtering,
    # where the pre-existing our_login-last-reply skip takes over.
    gated, ungated = _split_gated_focused_review_comments([_ADDRESSED_MARKED_COMMENT], enabled=True)
    assert gated == []
    assert ungated == [_ADDRESSED_MARKED_COMMENT]


def test_focused_review_approved_true_when_reaction_present():
    with patch("harness.runners.address_comments.reply_has_reaction_from", return_value=True) as mock_reaction:
        result = _focused_review_approved(_MARKED_COMMENT, "acme/frontend", "alexei", {})
    assert result is True
    mock_reaction.assert_called_once_with(99, "acme/frontend", "alexei", {})


def test_focused_review_approved_false_when_no_reaction():
    with patch("harness.runners.address_comments.reply_has_reaction_from", return_value=False):
        result = _focused_review_approved(_MARKED_COMMENT, "acme/frontend", "alexei", {})
    assert result is False


def test_focused_review_approved_false_when_no_our_login():
    result = _focused_review_approved(_MARKED_COMMENT, "acme/frontend", None, {})
    assert result is False


def test_focused_review_approved_false_when_marker_reply_is_not_last():
    # _focused_review_approved should not even be relevant here since
    # _split_gated_focused_review_comments already excludes this comment from "gated" —
    # but verify directly too, since it's a separate function with its own contract.
    result = _focused_review_approved(_ADDRESSED_MARKED_COMMENT, "acme/frontend", "alexei", {})
    assert result is False


def test_filter_comments_gate_disabled_keeps_marked_comment_subject_to_normal_skip():
    # our_login authored the marker reply, so with the gate disabled the existing
    # our_login-last-reply skip still swallows it (today's accidental behavior).
    with patch("harness.runners.address_comments._get_unresolved_comment_ids", return_value=None):
        result = address_comments._filter_comments(
            [_MARKED_COMMENT], 1, "acme/frontend", "alexei", {}, require_reaction_for_focused_review=False
        )
    assert result == []


def test_filter_comments_gate_enabled_holds_back_unapproved_marked_comment():
    with (
        patch("harness.runners.address_comments._get_unresolved_comment_ids", return_value=None),
        patch("harness.runners.address_comments.reply_has_reaction_from", return_value=False),
    ):
        result = address_comments._filter_comments(
            [_MARKED_COMMENT], 1, "acme/frontend", "alexei", {}, require_reaction_for_focused_review=True
        )
    assert result == []


def test_filter_comments_gate_enabled_admits_approved_marked_comment():
    with (
        patch("harness.runners.address_comments._get_unresolved_comment_ids", return_value=None),
        patch("harness.runners.address_comments.reply_has_reaction_from", return_value=True),
    ):
        result = address_comments._filter_comments(
            [_MARKED_COMMENT], 1, "acme/frontend", "alexei", {}, require_reaction_for_focused_review=True
        )
    assert result == [_MARKED_COMMENT]


def test_filter_comments_gate_enabled_does_not_affect_plain_comments():
    with patch("harness.runners.address_comments._get_unresolved_comment_ids", return_value=None):
        result = address_comments._filter_comments(
            [_PLAIN_COMMENT], 1, "acme/frontend", "alexei", {}, require_reaction_for_focused_review=True
        )
    assert result == [_PLAIN_COMMENT]


def test_filter_comments_gate_enabled_does_not_reprocess_already_addressed_thread():
    # End-to-end: with the gate enabled, a thread whose marker reply has already been
    # superseded by our own completion reply must be dropped by the ordinary
    # our_login-last-reply skip, not resent to the backend again.
    with patch("harness.runners.address_comments._get_unresolved_comment_ids", return_value=None):
        result = address_comments._filter_comments(
            [_ADDRESSED_MARKED_COMMENT], 1, "acme/frontend", "alexei", {}, require_reaction_for_focused_review=True
        )
    assert result == []


def test_filter_comments_plugin_prefix_drops_inline_comments_outside_subdir():
    in_subdir = {**_PLAIN_COMMENT, "path": "plugins/foo/b.py"}
    outside_subdir = {**_MARKED_COMMENT, "path": "a.py", "author": "sonar", "replies": []}
    with patch("harness.runners.address_comments._get_unresolved_comment_ids", return_value=None):
        result = address_comments._filter_comments(
            [in_subdir, outside_subdir], 1, "acme/frontend", None, {}, plugin_prefix="plugins/foo"
        )
    assert result == [in_subdir]


def test_filter_comments_plugin_prefix_keeps_non_inline_comments():
    issue_comment = {"type": "issue", "id": 3, "author": "bob", "body": "please fix", "url": "http://z"}
    with patch("harness.runners.address_comments._get_unresolved_comment_ids", return_value=None):
        result = address_comments._filter_comments(
            [issue_comment], 1, "acme/frontend", None, {}, plugin_prefix="plugins/foo"
        )
    assert result == [issue_comment]


def test_address_single_comment_passes_opencode_dir_to_backend():
    mock_backend = MagicMock()
    mock_backend.run.return_value = MagicMock(returncode=0)
    with patch("harness.runners.address_comments.get_head_sha", return_value="sha-before"):
        _address_single_comment(
            _FAKE_COMMENT,
            1,
            "instructions",
            mock_backend,
            "/repo",
            "acme/frontend",
            {},
            "sha-before",
            opencode_dir="/repo/plugins/foo",
        )
    mock_backend.run.assert_called_once_with(ANY, cwd="/repo", opencode_dir="/repo/plugins/foo")


def _graphql_page(nodes, cursor=None, has_next=False):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"endCursor": cursor, "hasNextPage": has_next},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def test_get_unresolved_comment_ids_paginates_across_multiple_pages():
    page1 = _graphql_page(
        [{"isResolved": False, "comments": {"nodes": [{"databaseId": 1}]}}], cursor="CURSOR1", has_next=True
    )
    page2 = _graphql_page([{"isResolved": False, "comments": {"nodes": [{"databaseId": 2}]}}])
    responses = iter([
        MagicMock(returncode=0, stdout=json.dumps(page1).encode()),
        MagicMock(returncode=0, stdout=json.dumps(page2).encode()),
    ])
    with patch("harness.runners.address_comments.run_cmd", side_effect=lambda *a, **k: next(responses)):
        ids = address_comments._get_unresolved_comment_ids(1, "acme/frontend", {})
    assert ids == {1, 2}


def test_has_pending_feedback_finds_unresolved_thread_on_second_page():
    page1 = _graphql_page([{"isResolved": True}], cursor="CURSOR1", has_next=True)
    page2 = _graphql_page([{"isResolved": False}])
    responses = iter([
        MagicMock(returncode=0, stdout=json.dumps(page1).encode()),
        MagicMock(returncode=0, stdout=json.dumps(page2).encode()),
    ])
    with patch("harness.runners.address_comments.run_cmd", side_effect=lambda *a, **k: next(responses)):
        result = address_comments._has_pending_feedback(1, "acme/frontend", {})
    assert result is True
