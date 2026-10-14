import json
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from harness.config import FocusedReviewConfig, HarnessConfig, HarnessSection, RepoConfig, VibehealConfig
from harness.runners import common, focused_review
from harness.runners.focused_review import (
    FOCUSED_REVIEW_MARKER,
    _build_prompt,
    _matching_comments,
    _resolve_knowledge_file,
)

_URL = (
    "https://raw.githubusercontent.com/jpablo/vibe-types/"
    "7891def9e1b66bebd95a393b42f3401eba697cd5/"
    "plugin/skills/typescript/catalog/T02-union-intersection.md"
)


def test_matches_inline_comment_with_knowledge_url():
    comments = [{"type": "inline", "body": f"Flatten into a single union instead. See: {_URL}", "replies": []}]
    result = _matching_comments(comments)
    assert len(result) == 1
    _comment, commit, path = result[0]
    assert commit == "7891def9e1b66bebd95a393b42f3401eba697cd5"
    assert path == "plugin/skills/typescript/catalog/T02-union-intersection.md"


def test_ignores_url_with_trailing_sentence_punctuation():
    comments = [{"type": "inline", "body": f"See: {_URL}.", "replies": []}]
    result = _matching_comments(comments)
    assert len(result) == 1
    _comment, _commit, path = result[0]
    assert path == "plugin/skills/typescript/catalog/T02-union-intersection.md"


def test_ignores_non_inline_comments():
    comments = [{"type": "review", "body": _URL, "replies": []}]
    assert _matching_comments(comments) == []


def test_ignores_comments_without_knowledge_url():
    comments = [{"type": "inline", "body": "just SonarQube noise", "replies": []}]
    assert _matching_comments(comments) == []


def test_skips_comment_already_marked():
    comments = [
        {
            "type": "inline",
            "body": _URL,
            "replies": [{"author": "bot", "body": f"some reply\n\n{FOCUSED_REVIEW_MARKER}"}],
        }
    ]
    assert _matching_comments(comments) == []


def test_does_not_skip_when_replies_lack_marker():
    comments = [
        {
            "type": "inline",
            "body": _URL,
            "replies": [{"author": "alice", "body": "unrelated reply"}],
        }
    ]
    assert len(_matching_comments(comments)) == 1


def _target(**extra):
    return {"type": "inline", "id": 1, "body": _URL, "replies": [], **extra}


def test_skips_comment_in_resolved_thread():
    assert _matching_comments([_target(thread_resolved=True)]) == []


def test_skips_minimised_comment_in_unresolved_thread():
    assert _matching_comments([_target(thread_resolved=False, is_minimized=True)]) == []


def test_skips_resolved_thread_that_also_has_marker_reply():
    replies = [{"author": "bot", "body": f"x\n\n{FOCUSED_REVIEW_MARKER}"}]
    assert _matching_comments([_target(thread_resolved=True, replies=replies)]) == []


def test_comment_without_thread_state_keys_still_matches():
    assert len(_matching_comments([_target()])) == 1


def test_recreated_comment_matched_next_to_old_resolved_comment():
    old = _target(id=1, thread_resolved=True, is_minimized=False)
    new = _target(id=2, thread_resolved=False, is_minimized=False)
    result = _matching_comments([old, new])
    assert [c["id"] for c, _commit, _path in result] == [2]


def test_resolve_knowledge_file_commit_hit_first_try():
    with patch("harness.runners.focused_review.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"content")
        result = _resolve_knowledge_file(Path("/vt"), "abc123", "catalog/T02.md", {})
    assert result == "content"
    assert mock_run.call_count == 1


def test_resolve_knowledge_file_fetch_then_retry_hit():
    responses = [
        MagicMock(returncode=1, stdout=b""),
        MagicMock(returncode=0, stdout=b""),
        MagicMock(returncode=0, stdout=b"content"),
    ]
    with patch("harness.runners.focused_review.run_cmd", side_effect=responses) as mock_run:
        result = _resolve_knowledge_file(Path("/vt"), "abc123", "catalog/T02.md", {})
    assert result == "content"
    assert mock_run.call_count == 3
    calls = mock_run.call_args_list
    assert calls[1].args[0] == ["git", "fetch", "origin", "abc123"]


def test_resolve_knowledge_file_falls_back_to_main():
    responses = [
        MagicMock(returncode=1, stdout=b""),
        MagicMock(returncode=0, stdout=b""),
        MagicMock(returncode=1, stdout=b""),
        MagicMock(returncode=0, stdout=b""),
        MagicMock(returncode=0, stdout=b"main content"),
    ]
    with patch("harness.runners.focused_review.run_cmd", side_effect=responses) as mock_run:
        result = _resolve_knowledge_file(Path("/vt"), "abc123", "catalog/T02.md", {})
    assert result == "main content"
    assert mock_run.call_count == 5
    calls = mock_run.call_args_list
    assert calls[0].args[0] == ["git", "show", "abc123:catalog/T02.md"]
    assert calls[1].args[0] == ["git", "fetch", "origin", "abc123"]
    assert calls[2].args[0] == ["git", "show", "abc123:catalog/T02.md"]
    assert calls[3].args[0] == ["git", "fetch", "origin", "main"]
    assert calls[4].args[0] == ["git", "show", "origin/main:catalog/T02.md"]


def test_resolve_knowledge_file_returns_none_when_all_fail():
    with patch("harness.runners.focused_review.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        result = _resolve_knowledge_file(Path("/vt"), "abc123", "catalog/T02.md", {})
    assert result is None


def test_build_prompt_includes_all_sections():
    comment = {
        "path": "src/types.ts",
        "line": 12,
        "id": 42,
        "url": "http://x",
        "body": "Type alias 'X' is a union of other union aliases. See: http://k",
        "diff_hunk": "@@ -1,2 +1,2 @@\n-old\n+new",
    }
    prompt = _build_prompt("INSTRUCTIONS", comment, "KNOWLEDGE TEXT", 7, "acme/repo")
    assert "INSTRUCTIONS" in prompt
    assert "src/types.ts:12" in prompt
    assert "Comment ID: 42" in prompt
    assert "@@ -1,2 +1,2 @@" in prompt
    assert "KNOWLEDGE TEXT" in prompt
    assert "PR number: 7" in prompt
    assert "Repo: acme/repo" in prompt
    assert "Marker: [focused-review-bot]" in prompt


def test_build_prompt_omits_diff_hunk_section_when_absent():
    comment = {"path": "a.ts", "line": 1, "id": 1, "url": "", "body": "b", "diff_hunk": ""}
    prompt = _build_prompt("T", comment, "K", 1, "r")
    assert "Diff context:" not in prompt


def test_build_prompt_renders_question_mark_when_line_is_none():
    comment = {"path": "a.ts", "line": None, "id": 1, "url": "", "body": "b", "diff_hunk": ""}
    prompt = _build_prompt("T", comment, "K", 1, "r")
    assert "File: a.ts:?" in prompt


def _id_comment(**extra):
    return {"path": "a.ts", "line": 1, "id": 1, "url": "", "body": "b", "diff_hunk": "", **extra}


def test_build_prompt_includes_node_ids_outside_external_data():
    prompt = _build_prompt("T", _id_comment(thread_id="PRRT_1", node_id="PRRC_1"), "K", 1, "r")
    assert "Thread node ID: PRRT_1" in prompt
    assert "Comment node ID: PRRC_1" in prompt
    first_tag = prompt.index("<!-- EXTERNAL DATA BEGIN")
    body_tag = prompt.index("<pr-comment-body>")
    for line in ("Thread node ID: PRRT_1", "Comment node ID: PRRC_1"):
        assert prompt.index(line) < first_tag
        assert prompt.index(line) < body_tag


@pytest.mark.parametrize("extra", [{"thread_id": "", "node_id": None}, {}])
def test_build_prompt_renders_question_mark_for_empty_or_absent_node_ids(extra):
    prompt = _build_prompt("T", _id_comment(**extra), "K", 1, "r")
    assert "Thread node ID: ?\n" in prompt
    assert "Comment node ID: ?\n" in prompt


def test_build_prompt_with_only_one_node_id_key_does_not_raise():
    prompt = _build_prompt("T", _id_comment(node_id="PRRC_9"), "K", 1, "r")
    assert "Thread node ID: ?" in prompt
    assert "Comment node ID: PRRC_9" in prompt


def test_build_prompt_includes_only_processed_comment_ids():
    other = _id_comment(id=2, thread_id="PRRT_OTHER", node_id="PRRC_OTHER")
    mine = _id_comment(thread_id="PRRT_MINE", node_id="PRRC_MINE")
    prompt = _build_prompt("T", mine, "K", 1, "r")
    assert other["thread_id"] not in prompt
    assert other["node_id"] not in prompt
    assert "PRRT_MINE" in prompt
    assert "PRRC_MINE" in prompt


def test_focused_review_source_has_no_graphql_mutation_names():
    source = Path(focused_review.__file__).read_text()
    assert "resolveReviewThread" not in source
    assert "minimizeComment" not in source


def _cfg(tmp_path, enabled=True):
    return HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        vibe_heal=VibehealConfig(),
        focused_review=FocusedReviewConfig(enabled=enabled, vibe_types_repo=tmp_path / "vt"),
    )


def _setup_knowledge(tmp_path):
    d = tmp_path / "k" / "pr-review"
    d.mkdir(parents=True, exist_ok=True)
    (d / "focused-review.md").write_text("instructions")


_MATCHING_COMMENT = {
    "type": "inline",
    "id": 1,
    "path": "a.ts",
    "line": 5,
    "url": "http://x",
    "body": f"Flatten into a single union instead. See: {_URL}",
    "diff_hunk": "",
    "replies": [],
}


def test_disabled_skips_everything(tmp_path):
    cfg = _cfg(tmp_path, enabled=False)
    with patch("harness.runners.focused_review.get_gh_token") as mock_token:
        focused_review._run_locked(cfg)
    mock_token.assert_not_called()


def test_matching_comment_invokes_backend(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_called_once()


def test_re_requests_review_when_previously_requested(tmp_xdg, tmp_path):
    """Replying to a comment can submit a review via the GitHub API as a side effect,
    which clears the submitter from the PR's requested-reviewers list — hiding it from
    review_requested's "user-review-requested:@me" search. If the user was already a
    requested reviewer before this cycle, they must be re-added afterward — mirrors
    review_prs.py's add_reviewer guard."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
        patch("harness.runners.common.was_review_requested", return_value=True) as mock_was_requested,
        patch("harness.runners.common.add_reviewer") as mock_add_reviewer,
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_called_once()
    mock_was_requested.assert_called_once_with(1, "acme/frontend", "bot", ANY)
    mock_add_reviewer.assert_called_once_with(1, "acme/frontend", "bot", mock_add_reviewer.call_args.args[3])


def test_does_not_re_request_review_if_not_previously_requested(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.common.add_reviewer") as mock_add_reviewer,
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend"),
    ):
        focused_review._run_locked(cfg)
    mock_add_reviewer.assert_not_called()


def test_already_marked_comment_skips_backend(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    marked = {
        **_MATCHING_COMMENT,
        "replies": [{"author": "bot", "body": f"done\n\n{focused_review.FOCUSED_REVIEW_MARKER}"}],
    }
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[marked]),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout") as mock_checkout,
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_checkout.assert_not_called()


def test_non_matching_comment_skips_backend(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    plain = {**_MATCHING_COMMENT, "body": "just SonarQube noise, no knowledge url"}
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[plain]),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()


def test_non_matching_pr_skips_restore(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    plain = {**_MATCHING_COMMENT, "body": "just SonarQube noise, no knowledge url"}
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[plain]),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore") as mock_restore,
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_restore.assert_not_called()


def test_unresolvable_knowledge_file_skips_comment_but_continues(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore") as mock_restore,
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value=None),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_restore.assert_called_once()


def test_restores_working_dir_even_on_backend_failure(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore") as mock_restore,
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.side_effect = Exception("backend exploded")
        focused_review._run_locked(cfg)
    mock_restore.assert_called_once()


def test_fatal_git_error_continues_to_remaining_prs(tmp_xdg, tmp_path):
    # A fatal git error on one PR must not abort the remaining PRs, since
    # the finally block restores the repo state before moving on.
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}, {"number": 2, "headRefName": "c"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch(
            "harness.runners.focused_review.git_fetch_and_checkout",
            side_effect=focused_review.FatalGitError("boom"),
        ) as mock_checkout,
        patch("harness.runners.focused_review.git_restore") as mock_restore,
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    assert mock_checkout.call_count == 2
    assert mock_restore.call_count == 2
    mock_be.return_value.run.assert_not_called()


def test_non_fatal_error_continues_to_next_pr(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}, {"number": 2, "headRefName": "c"}],
        ),
        patch(
            "harness.runners.focused_review.fetch_pr_comments",
            side_effect=[RuntimeError("boom"), [_MATCHING_COMMENT]],
        ),
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_called_once()


@pytest.fixture(autouse=True)
def _no_real_thread_fetch():
    """Shared patch so _run_locked tests never shell out to `gh api graphql`."""
    with patch("harness.runners.focused_review.fetch_review_threads", return_value=[]) as m:
        yield m


def _thread(thread_id, resolved, comments):
    return {
        "id": thread_id,
        "isResolved": resolved,
        "comments": {"nodes": [{"id": n, "databaseId": d, "isMinimized": m} for n, d, m in comments]},
    }


def _page(nodes, has_next=False, cursor=None):
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}
                }
            }
        }
    }
    return MagicMock(returncode=0, stdout=json.dumps(payload).encode())


def test_merge_single_page():
    comments = [{"type": "inline", "id": 10}]
    common.merge_thread_state(comments, [_thread("T1", True, [("C10", 10, True), ("C11", 11, False)])])
    assert comments[0] == {
        "type": "inline",
        "id": 10,
        "thread_id": "T1",
        "thread_resolved": True,
        "node_id": "C10",
        "is_minimized": True,
    }


def test_merge_unresolved_thread_sets_false_values():
    comments = [{"type": "inline", "id": 10}]
    common.merge_thread_state(comments, [_thread("T1", False, [("C10", 10, False)])])
    assert comments[0]["thread_resolved"] is False
    assert comments[0]["is_minimized"] is False


def test_merge_matches_only_root_comment():
    comments = [{"type": "inline", "id": 11}]
    common.merge_thread_state(comments, [_thread("T1", False, [("C10", 10, False), ("C11", 11, False)])])
    assert "thread_id" not in comments[0]


def test_merge_thread_root_not_in_comments_is_ignored():
    comments = [{"type": "inline", "id": 10}]
    common.merge_thread_state(comments, [_thread("T9", False, [("C99", 99, False)])])
    assert comments == [{"type": "inline", "id": 10}]


def test_merge_comment_not_in_any_thread_gets_no_keys():
    comments = [{"type": "inline", "id": 10}]
    common.merge_thread_state(comments, [])
    assert comments == [{"type": "inline", "id": 10}]


def test_merge_non_inline_untouched():
    comments = [{"type": "review", "id": 10}, {"type": "issue", "id": 10}]
    common.merge_thread_state(comments, [_thread("T1", True, [("C10", 10, False)])])
    assert comments == [{"type": "review", "id": 10}, {"type": "issue", "id": 10}]


def test_fetch_review_threads_follows_pagination():
    pages = [
        _page([_thread("T1", False, [("C1", 1, False)])], has_next=True, cursor="CUR1"),
        _page([_thread("T2", True, [("C2", 2, False)])]),
    ]
    with patch("harness.runners.common.run_cmd", side_effect=pages) as mock_run:
        nodes = common.fetch_review_threads(7, "acme/frontend", {})
    assert nodes is not None
    assert [n["id"] for n in nodes] == ["T1", "T2"]
    first, second = (c.args[0] for c in mock_run.call_args_list)
    assert "cursor=CUR1" not in " ".join(first)
    assert second[-2:] == ["-f", "cursor=CUR1"]
    assert "owner=acme" in first
    assert "repo=frontend" in first
    assert "number=7" in first


@pytest.mark.parametrize(
    "result",
    [
        MagicMock(returncode=1, stdout=b""),
        MagicMock(returncode=0, stdout=b"not json"),
        MagicMock(returncode=0, stdout=b"{}"),
        MagicMock(returncode=0, stdout=b'{"data": {"repository": {"pullRequest": null}}}'),
    ],
)
def test_fetch_review_threads_failure_returns_none(result):
    with patch("harness.runners.common.run_cmd", return_value=result):
        assert common.fetch_review_threads(7, "acme/frontend", {}) is None


def test_fetch_review_threads_failure_on_later_page_returns_none():
    pages = [_page([], has_next=True, cursor="C"), MagicMock(returncode=1, stdout=b"")]
    with patch("harness.runners.common.run_cmd", side_effect=pages):
        assert common.fetch_review_threads(7, "acme/frontend", {}) is None


def _run_with_threads(tmp_path, comments, threads):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=comments),
        patch("harness.runners.focused_review.fetch_review_threads", return_value=threads) as mock_threads,
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout") as mock_checkout,
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    return mock_threads, mock_checkout, mock_be


def test_thread_fetch_failure_skips_pr(tmp_xdg, tmp_path, caplog):
    mock_threads, mock_checkout, mock_be = _run_with_threads(tmp_path, [dict(_MATCHING_COMMENT)], None)
    mock_threads.assert_called_once()
    mock_checkout.assert_not_called()
    mock_be.return_value.run.assert_not_called()
    assert "PR #1" in caplog.text


def test_thread_fetch_failure_does_not_stop_other_prs(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
        patch("harness.runners.focused_review.get_current_user", return_value="bot"),
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}, {"number": 2, "headRefName": "c"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[dict(_MATCHING_COMMENT)]),
        patch("harness.runners.focused_review.fetch_review_threads", side_effect=[None, []]),
        patch("harness.runners.common.was_review_requested", return_value=False),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_called_once()


def test_no_target_comment_makes_no_thread_fetch(tmp_xdg, tmp_path):
    plain = {**_MATCHING_COMMENT, "body": "no knowledge url"}
    mock_threads, _checkout, _be = _run_with_threads(tmp_path, [plain], [])
    mock_threads.assert_not_called()


def test_run_merges_thread_state_into_target_comment(tmp_xdg, tmp_path):
    comment = dict(_MATCHING_COMMENT)
    threads = [_thread("T1", False, [("C1", 1, False)])]
    _t, _c, mock_be = _run_with_threads(tmp_path, [comment], threads)
    assert comment["thread_id"] == "T1"
    assert comment["node_id"] == "C1"
    mock_be.return_value.run.assert_called_once()


def test_target_without_matching_thread_is_still_processed(tmp_xdg, tmp_path):
    comment = dict(_MATCHING_COMMENT)
    _t, _c, mock_be = _run_with_threads(tmp_path, [comment], [])
    assert "thread_id" not in comment
    mock_be.return_value.run.assert_called_once()


def test_resolved_thread_target_skips_backend_end_to_end(tmp_xdg, tmp_path):
    # Real flow: thread state comes from the (patched) GraphQL fetch and is merged in
    # _run_locked, so the skip must act on the merged keys, not pre-set ones.
    comment = dict(_MATCHING_COMMENT)
    threads = [_thread("T1", True, [("C1", 1, False)])]
    mock_threads, mock_checkout, mock_be = _run_with_threads(tmp_path, [comment], threads)
    mock_threads.assert_called_once()
    mock_be.return_value.run.assert_not_called()
    mock_checkout.assert_not_called()


def test_minimised_target_skips_backend_end_to_end(tmp_xdg, tmp_path):
    comment = dict(_MATCHING_COMMENT)
    threads = [_thread("T1", False, [("C1", 1, True)])]
    _t, mock_checkout, mock_be = _run_with_threads(tmp_path, [comment], threads)
    mock_be.return_value.run.assert_not_called()
    mock_checkout.assert_not_called()


def test_runner_passes_node_ids_to_backend_prompt(tmp_xdg, tmp_path):
    comment = dict(_MATCHING_COMMENT)
    threads = [_thread("PRRT_T1", False, [("PRRC_C1", 1, False)])]
    _t, _c, mock_be = _run_with_threads(tmp_path, [comment], threads)
    mock_be.return_value.run.assert_called_once()
    prompt = mock_be.return_value.run.call_args.args[0]
    assert "Thread node ID: PRRT_T1" in prompt
    assert "Comment node ID: PRRC_C1" in prompt


# --- Template string assertions (read the real template at the repo root) ---

_TEMPLATE_PATH = Path(__file__).resolve().parents[4] / "knowledge" / "pr-review" / "focused-review.md"

_RESOLVE_MUTATION = (
    "gh api graphql -f query='mutation($threadId:ID!){ resolveReviewThread(input:{threadId:$threadId})"
    "{ thread{ id isResolved } } }' -f threadId=<THREAD_ID>"
)
_MINIMIZE_MUTATION = (
    "gh api graphql -f query='mutation($subjectId:ID!){ minimizeComment(input:{subjectId:$subjectId, "
    "classifier:RESOLVED}){ minimizedComment{ isMinimized minimizedReason } } }' -f subjectId=<COMMENT_NODE_ID>"
)


def _template() -> str:
    return _TEMPLATE_PATH.read_text()


def test_template_path_is_real_file():
    assert _TEMPLATE_PATH.is_file()


def test_template_defines_false_positive_outcome_for_all_causes():
    text = " ".join(_template().split())
    assert "False positive" in text
    assert "never valid" in text
    assert "misfired" in text
    assert "already been refactored or the finding no longer applies" in text
    assert "The cause does not matter" in text
    assert "post a brief reply saying so" not in text


def test_template_has_both_mutations_verbatim_in_order():
    text = _template()
    assert _RESOLVE_MUTATION in text
    assert _MINIMIZE_MUTATION in text
    assert text.index("resolveReviewThread") < text.index("minimizeComment")
    assert "classifier:RESOLVED" in text
    assert text.index(_RESOLVE_MUTATION) < text.index(_MINIMIZE_MUTATION)


def test_template_false_positive_posts_nothing_and_refactor_path_unchanged():
    text = " ".join(_template().split())
    assert "Post no reply and no other text" in text
    assert "## Step 3 — Write the refactor description" in text
    assert "## Step 4 — Post the reply" in text
    assert '-X POST -f body="<your refactor description>' in text
    assert "<Marker>" in text
    assert "MUST include the marker verbatim" in text
    assert "A false positive posts no reply" in text
    assert "Never request or re-request a review." in text


def test_template_applies_to_root_comment_and_never_deletes():
    text = " ".join(_template().split())
    assert "root comment even if the thread has human replies" in text
    for forbidden in ("DELETE", "-X DELETE", "deleteComment"):
        assert forbidden not in text


def test_template_has_no_confidence_gate_or_record_keeping_wording():
    lowered = _template().lower()
    for word in ("confidence", "threshold", "audit", "log file", "record of"):
        assert word not in lowered


# --- Safety rails, failure handling, criterion reuse (A-3c) ---


def _false_positive_text() -> str:
    text = " ".join(_template().split())
    start = text.index("**False positive.**")
    end = text.index("- Never request or re-request a review.")
    return text[start:end]


def test_template_keeps_safety_phrases_and_untrusted_statement():
    text = " ".join(_template().split())
    assert "Do not edit any file, run `git add`, commit, or push" in text
    assert "Never request or re-request a review" in text
    assert "untrusted data" in text
    assert "comment body" in text and "diff" in text and "knowledge file" in text


def test_template_no_code_change_statement_covers_resolve_and_minimise():
    text = " ".join(_template().split())
    assert "This step only posts a comment" in text  # prefix of the new wording
    assert "This step only posts a comment —" not in text
    assert "This step only posts a comment, resolves the thread, or minimises the comment" in text
    assert "it never changes code" in text


def test_template_states_mutation_failure_handling():
    text = " ".join(_template().split())
    start = text.index("**If a mutation fails**")
    section = text[start:]
    assert "retry in a loop" in section
    assert "fallback reply" in section
    assert "finish" in section
    assert "never an error of the comment" in section
    assert "still attempt the minimise mutation" in section
    assert "still attempt the resolve mutation" in section
    assert "regardless" in section
    assert "The verdict is final" in text
    assert "even if the thread has human replies" in text


def test_template_false_positive_reuses_criterion_without_explain_instruction():
    fp = _false_positive_text()
    assert "you believe the tool is wrong" in fp
    assert "explain" not in fp.lower()
    # The refactor path legitimately describes/explains; the check is scoped to the false-positive text.
    assert "resolveReviewThread" in fp and "minimizeComment" in fp
