"""Tests for GitHubReviewClient."""

import json
from unittest.mock import AsyncMock

import pytest

from dotharness.subprocess_runner import RunResult
from dotharness.vibe_heal.review.github import (
    GitHubReviewClient,
    GitHubReviewError,
    GitHubTimeoutError,
)
from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult


def _patch_run(mocker, *results: tuple[int, str, str]) -> AsyncMock:
    """Patch the shared runner used by _post_json; one (returncode, stdout, stderr) per call."""
    return mocker.patch(
        "dotharness.vibe_heal.review.github.run",
        new_callable=AsyncMock,
        side_effect=[RunResult(*r) for r in results],
    )


@pytest.fixture
def sample_report() -> ReviewResult:
    """Create a sample ReviewResult for testing."""
    return ReviewResult(
        project_key="test-project",
        branch="feature/test",
        base_branch="origin/main",
        files=[
            FileReview(
                file_path="src/example.py",
                issues=[
                    ReviewIssue(
                        rule="python:S1481",
                        message="Remove unused variable",
                        line=10,
                        severity="MAJOR",
                        doc_url="https://rules.example.com/S1481",
                    ),
                    ReviewIssue(
                        rule="python:S1192",
                        message="Useless import",
                        line=25,
                        severity="MINOR",
                    ),
                ],
            ),
        ],
    )


class TestValidateInstalled:
    """Tests for validate_installed."""

    @pytest.mark.asyncio
    async def test_raises_when_gh_not_installed(self, mocker) -> None:
        """validate_installed raises OSError when gh is not installed."""
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=False),
        )

        client = GitHubReviewClient()
        with pytest.raises(OSError, match="gh CLI"):
            await client.validate_installed()


class TestDetectPr:
    """Tests for detect_pr."""

    @pytest.mark.asyncio
    async def test_uses_explicit_number(self) -> None:
        """detect_pr returns the explicit PR number when provided."""
        client = GitHubReviewClient()
        result = await client.detect_pr(pr_number=42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_auto_detects_from_current_branch(self, sample_report, mocker) -> None:
        """detect_pr auto-detects PR number from gh pr view."""
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(
                success=True,
                stdout='{"number": 73}',
            ),
        )

        client = GitHubReviewClient()
        pr_number = await client.detect_pr()
        assert pr_number == 73

    @pytest.mark.asyncio
    async def test_raises_when_not_authenticated(self, mocker) -> None:
        """detect_pr raises GitHubReviewError when gh is not authenticated."""

        async def _run(cmd, **kwargs):
            if "--version" in cmd:
                return mocker.MagicMock(success=True, stdout="gh version 2.52.0")
            return mocker.MagicMock(success=False, stderr="error: you are not authenticated")

        mocker.patch("dotharness.vibe_heal.review.github.run_command", side_effect=_run)

        client = GitHubReviewClient()
        with pytest.raises(GitHubReviewError, match="authenticated"):
            await client.detect_pr()


class TestDetectPrBaseBranch:
    """Tests for detect_pr_base_branch."""

    @pytest.mark.asyncio
    async def test_returns_base_ref_when_gh_succeeds(self, mocker) -> None:
        """detect_pr_base_branch returns the baseRefName from gh pr view."""
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(
                success=True,
                stdout='{"baseRefName": "feature/lower-in-stack"}',
            ),
        )

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result == "feature/lower-in-stack"

    @pytest.mark.asyncio
    async def test_returns_main_when_pr_targets_main(self, mocker) -> None:
        """Non-stacked PRs (base is main) resolve the same as any other base."""
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(
                success=True,
                stdout='{"baseRefName": "main"}',
            ),
        )

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result == "main"

    @pytest.mark.asyncio
    async def test_returns_none_when_gh_pr_view_fails(self, mocker) -> None:
        """detect_pr_base_branch returns None when gh pr view fails."""
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=False),
        )

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_gh_not_installed(self, mocker) -> None:
        """detect_pr_base_branch returns None (never raises) when gh is missing."""
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("gh not found"),
        )

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_open_pr(self, mocker) -> None:
        """detect_pr_base_branch returns None when gh pr view fails (no PR)."""

        async def _run(cmd, **kwargs):
            if "--version" in cmd:
                return mocker.MagicMock(success=True, stdout="gh version 2.52.0")
            return mocker.MagicMock(success=False, stderr="no pull requests found")

        mocker.patch("dotharness.vibe_heal.review.github.run_command", side_effect=_run)

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_malformed_json(self, mocker) -> None:
        """detect_pr_base_branch returns None when gh returns unparseable JSON."""

        async def _run(cmd, **kwargs):
            if "--version" in cmd:
                return mocker.MagicMock(success=True, stdout="gh version 2.52.0")
            return mocker.MagicMock(success=True, stdout="not json")

        mocker.patch("dotharness.vibe_heal.review.github.run_command", side_effect=_run)

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_base_ref_name_missing(self, mocker) -> None:
        """detect_pr_base_branch returns None when the JSON has no baseRefName key."""

        async def _run(cmd, **kwargs):
            if "--version" in cmd:
                return mocker.MagicMock(success=True, stdout="gh version 2.52.0")
            return mocker.MagicMock(success=True, stdout="{}")

        mocker.patch("dotharness.vibe_heal.review.github.run_command", side_effect=_run)

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_json_is_not_an_object(self, mocker) -> None:
        """detect_pr_base_branch returns None when gh returns valid but non-object JSON."""

        async def _run(cmd, **kwargs):
            if "--version" in cmd:
                return mocker.MagicMock(success=True, stdout="gh version 2.52.0")
            return mocker.MagicMock(success=True, stdout="null")

        mocker.patch("dotharness.vibe_heal.review.github.run_command", side_effect=_run)

        client = GitHubReviewClient()
        result = await client.detect_pr_base_branch()
        assert result is None


class TestPostReview:
    """Tests for post_review."""

    @pytest.mark.asyncio
    async def test_single_api_call(self, sample_report, mocker) -> None:
        """post_review makes a single gh api POST call."""
        mock_run = _patch_run(mocker, (0, '{"id": 123}', ""))
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, sample_report)

        assert mock_run.await_count == 1
        stdin_json = json.loads(mock_run.call_args.kwargs["stdin"])
        assert stdin_json["event"] == "COMMENT"
        assert len(stdin_json["comments"]) == 2

    @pytest.mark.asyncio
    async def test_fallback_comment_for_rejected_lines(self, sample_report, mocker) -> None:
        """When inline review fails, post_review posts a top-level fallback comment."""
        mock_run = _patch_run(mocker, (1, '{"message": "Unprocessable Entity"}', ""), (0, "{}", ""))
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, sample_report)

        assert mock_run.await_count == 2
        fallback_json = json.loads(mock_run.call_args.kwargs["stdin"])
        assert fallback_json["event"] == "COMMENT"
        assert fallback_json["comments"] == []
        assert "python:S1481" in fallback_json["body"]
        assert "python:S1192" in fallback_json["body"]

    @pytest.mark.asyncio
    async def test_timeout_does_not_trigger_fallback(self, sample_report, mocker) -> None:
        """A timed-out inline post may have succeeded, so no fallback review is posted."""
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )
        client = GitHubReviewClient()
        post = mocker.patch.object(
            client,
            "_post_json",
            new_callable=AsyncMock,
            side_effect=GitHubTimeoutError("gh API call timed out after 60s"),
        )
        with pytest.raises(GitHubTimeoutError):
            await client.post_review(42, sample_report)
        assert post.await_count == 1

    @pytest.mark.asyncio
    async def test_raises_when_fallback_also_fails(self, sample_report, mocker) -> None:
        """post_review raises GitHubReviewError when both attempts fail."""
        _patch_run(
            mocker,
            (1, '{"message": "Server error"}', "internal error"),
            (1, '{"message": "Server error"}', "internal error"),
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        with pytest.raises(GitHubReviewError, match="failed"):
            await client.post_review(42, sample_report)

    @pytest.mark.asyncio
    async def test_empty_report_posts_empty_review(self, mocker) -> None:
        """post_review handles a report with no issues gracefully."""
        empty_report = ReviewResult(
            project_key="test",
            branch="main",
            base_branch="origin/main",
            files=[],
        )

        mock_run = _patch_run(mocker, (0, "{}", ""))
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(1, empty_report)

        stdin_json = json.loads(mock_run.call_args.kwargs["stdin"])
        assert stdin_json["event"] == "COMMENT"
        assert stdin_json["comments"] == []

    @pytest.mark.asyncio
    async def test_fallback_logs_warning_with_inline_error(self, sample_report, mocker, caplog) -> None:
        """post_review logs a warning with the inline error reason before falling back."""
        import logging

        _patch_run(mocker, (1, "", "validation error: line outside diff"), (0, "{}", ""))
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        with caplog.at_level(logging.WARNING, logger="dotharness.vibe_heal.review.github"):
            await client.post_review(42, sample_report)

        assert any("Inline review failed" in r.message for r in caplog.records)
        assert any("falling back" in r.message for r in caplog.records)
