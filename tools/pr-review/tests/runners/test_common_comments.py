import json
from unittest.mock import MagicMock, patch

from harness.runners.common import (
    FOCUSED_REVIEW_MARKER,
    fetch_pr_comments,
    find_reply_with_marker,
    reply_has_reaction_from,
)


def test_fetch_pr_comments_returns_empty_on_script_failure(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        result = fetch_pr_comments(1, tmp_path / "pr-comments.py", str(tmp_path), {})
    assert result == []


def test_fetch_pr_comments_returns_empty_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        result = fetch_pr_comments(1, tmp_path / "pr-comments.py", str(tmp_path), {})
    assert result == []


def test_fetch_pr_comments_tags_types_and_filters_bots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cache_dir = home / ".harness" / "cache"
    cache_dir.mkdir(parents=True)
    data = {
        "inline_comments": [{"id": 1, "body": "x"}],
        "review_comments": [{"id": "review-2", "body": "y"}],
        "issue_comments": [
            {"id": 3, "author": "alice", "body": "human comment"},
            {"id": 4, "author": "some-bot[bot]", "body": "bot comment"},
            {"id": 5, "author": "alice", "body": "mentions bot] inline"},
        ],
    }
    (cache_dir / "pr-9-comments.json").write_text(json.dumps(data))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        result = fetch_pr_comments(9, tmp_path / "pr-comments.py", str(tmp_path), {})
    types = {c["id"]: c["type"] for c in result}
    assert types[1] == "inline"
    assert types["review-2"] == "review"
    assert types[3] == "issue"
    assert 4 not in types
    assert 5 not in types


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


def test_reply_has_reaction_from_true_when_reaction_present():
    payload = [
        {"content": "heart", "user": {"login": "someone-else"}},
        {"content": "+1", "user": {"login": "alexei"}},
    ]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")
        result = reply_has_reaction_from(123, "acme/frontend", "alexei", {})
    assert result is True


def test_reply_has_reaction_from_false_when_wrong_user_reacted():
    payload = [{"content": "+1", "user": {"login": "someone-else"}}]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")
        result = reply_has_reaction_from(123, "acme/frontend", "alexei", {})
    assert result is False


def test_reply_has_reaction_from_false_when_only_other_reaction_type():
    payload = [{"content": "heart", "user": {"login": "alexei"}}]
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")
        result = reply_has_reaction_from(123, "acme/frontend", "alexei", {})
    assert result is False


def test_reply_has_reaction_from_fails_closed_on_api_error():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
        result = reply_has_reaction_from(123, "acme/frontend", "alexei", {})
    assert result is False


def test_reply_has_reaction_from_fails_closed_on_bad_json():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"not json", stderr=b"")
        result = reply_has_reaction_from(123, "acme/frontend", "alexei", {})
    assert result is False


def test_reply_has_reaction_from_fails_closed_on_non_list_json():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"message": "not found"}).encode(), stderr=b""
        )
        result = reply_has_reaction_from(123, "acme/frontend", "alexei", {})
    assert result is False
