from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.config import FocusedReviewConfig, HarnessConfig, HarnessSection, RepoConfig, VibehealConfig
from harness.runners import focused_review
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
    _comment, _commit, path = _matching_comments(comments)[0]
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
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
        patch("harness.runners.focused_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.focused_review.git_fetch_and_checkout"),
        patch("harness.runners.focused_review.git_restore"),
        patch("harness.runners.focused_review._resolve_knowledge_file", return_value="knowledge text"),
        patch("harness.runners.focused_review.Backend") as mock_be,
    ):
        focused_review._run_locked(cfg)
    mock_be.return_value.run.assert_called_once()


def test_already_marked_comment_skips_backend(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    marked = {
        **_MATCHING_COMMENT,
        "replies": [{"author": "bot", "body": f"done\n\n{focused_review.FOCUSED_REVIEW_MARKER}"}],
    }
    with (
        patch("harness.runners.focused_review.get_gh_token", return_value="tok"),
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
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
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
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
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
        patch(
            "harness.runners.focused_review.list_open_prs_for_current_user",
            return_value=[{"number": 1, "headRefName": "b"}, {"number": 2, "headRefName": "c"}],
        ),
        patch("harness.runners.focused_review.fetch_pr_comments", return_value=[_MATCHING_COMMENT]),
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
