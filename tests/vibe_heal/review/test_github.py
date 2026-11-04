"""Tests for GitHubReviewClient."""

import json
from unittest.mock import AsyncMock

import pytest

from dotharness.vibe_heal.review.github import _MAX_INLINE_COMMENTS, GitHubReviewClient, GitHubReviewError
from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult


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
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b'{"id": 123}', b"")
        mock_process.returncode = 0
        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            return_value=mock_process,
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, sample_report)

        assert mock_process.communicate.call_count == 1
        call_args = mock_process.communicate.call_args
        stdin_json = json.loads(call_args[0][0])
        assert stdin_json["event"] == "COMMENT"
        assert len(stdin_json["comments"]) == 2

    @pytest.mark.asyncio
    async def test_inline_comment_format(self, sample_report, mocker) -> None:
        """Inline comment body contains rule, message, and doc_url."""
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0
        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            return_value=mock_process,
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, sample_report)

        stdin_json = json.loads(mock_process.communicate.call_args[0][0])
        comment_with_url = stdin_json["comments"][0]
        assert comment_with_url["body"] == (
            "**python:S1481** Remove unused variable\n\nhttps://rules.example.com/S1481"
        )
        assert comment_with_url["path"] == "src/example.py"
        assert comment_with_url["line"] == 10

        comment_no_url = stdin_json["comments"][1]
        assert comment_no_url["body"] == "**python:S1192** Useless import"

    @pytest.mark.asyncio
    async def test_fallback_comment_for_rejected_lines(self, sample_report, mocker) -> None:
        """When inline review fails, post_review posts a top-level fallback comment."""
        mock_fail = AsyncMock()
        mock_fail.communicate.return_value = (
            b'{"message": "Unprocessable Entity"}',
            b"",
        )
        mock_fail.returncode = 1

        mock_success = AsyncMock()
        mock_success.communicate.return_value = (b"{}", b"")
        mock_success.returncode = 0

        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            side_effect=[mock_fail, mock_success],
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, sample_report)

        assert mock_fail.communicate.call_count == 1
        assert mock_success.communicate.call_count == 1
        fallback_json = json.loads(mock_success.communicate.call_args[0][0])
        assert fallback_json["event"] == "COMMENT"
        assert fallback_json["comments"] == []
        assert "python:S1481" in fallback_json["body"]
        assert "python:S1192" in fallback_json["body"]

    @pytest.mark.asyncio
    async def test_builds_correct_api_endpoint(self, sample_report, mocker) -> None:
        """post_review calls gh api with the correct repository endpoint."""
        mock_git = AsyncMock()
        mock_git.communicate.return_value = (b"git@github.com:myorg/myrepo.git\n", b"")
        mock_git.returncode = 0

        mock_api = AsyncMock()
        mock_api.communicate.return_value = (b"{}", b"")
        mock_api.returncode = 0

        mock_create = mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            side_effect=[mock_git, mock_api],
        )

        # validate_installed uses run_command; return success=True for --version
        # and success=False for gh repo view to trigger the git-remote fallback.
        def _run_cmd(cmd, **kwargs):
            if "--version" in cmd:
                return mocker.MagicMock(success=True, stdout="gh version 2.52.0")
            return mocker.MagicMock(success=False)

        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            side_effect=_run_cmd,
        )

        client = GitHubReviewClient()
        await client.post_review(42, sample_report)

        api_call_args = mock_create.call_args_list[1][0]
        assert api_call_args[0] == "gh"
        assert "api" in api_call_args
        assert "--method" in api_call_args
        assert "POST" in api_call_args
        assert "--input" in api_call_args
        assert "-" in api_call_args
        assert any("repos/myorg/myrepo/pulls/42/reviews" in str(a) for a in api_call_args)

    @pytest.mark.asyncio
    async def test_uses_gh_repo_for_owner_repo(self, sample_report, mocker) -> None:
        """post_review prefers gh CLI for owner/repo detection."""
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0
        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        mock_run = mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="ghorg/ghrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(99, sample_report)

        mock_run.assert_called()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "gh"

    @pytest.mark.asyncio
    async def test_raises_when_fallback_also_fails(self, sample_report, mocker) -> None:
        """post_review raises GitHubReviewError when both attempts fail."""
        mock_fail = AsyncMock()
        mock_fail.communicate.return_value = (
            b'{"message": "Server error"}',
            b"internal error",
        )
        mock_fail.returncode = 1

        mock_fail2 = AsyncMock()
        mock_fail2.communicate.return_value = (
            b'{"message": "Server error"}',
            b"internal error",
        )
        mock_fail2.returncode = 1

        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            side_effect=[mock_fail, mock_fail2],
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

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0
        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            return_value=mock_process,
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(1, empty_report)

        stdin_json = json.loads(mock_process.communicate.call_args[0][0])
        assert stdin_json["event"] == "COMMENT"
        assert stdin_json["comments"] == []

    @pytest.mark.asyncio
    async def test_root_cause_in_inline_comment_and_review_body(self, mocker) -> None:
        """Root-cause <details> blocks appear in each inline comment body, but are not duplicated in the review body (which contains only the summary)."""
        report_with_root_cause = ReviewResult(
            project_key="test",
            branch="feat",
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
                            root_cause="<p>Unused variables waste memory.</p>",
                        ),
                        ReviewIssue(
                            rule="python:S1481",
                            message="Another unused variable",
                            line=20,
                            severity="MAJOR",
                            root_cause="<p>Unused variables waste memory.</p>",
                        ),
                    ],
                ),
            ],
        )

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0
        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            return_value=mock_process,
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, report_with_root_cause)

        payload = json.loads(mock_process.communicate.call_args[0][0])
        # root_cause IS in each individual inline comment body
        for comment in payload["comments"]:
            assert "<details>" in comment["body"]
            assert "why this matters" in comment["body"]
        # top-level review body is just the summary — no duplicate rule details
        assert "<details>" not in payload["body"]
        assert "SonarQube:" in payload["body"]

    @pytest.mark.asyncio
    async def test_inline_payload_includes_nonempty_body(self, sample_report, mocker) -> None:
        """Inline review payload always includes a non-empty body (GitHub rejects empty body for COMMENT reviews)."""
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0
        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            return_value=mock_process,
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, sample_report)

        stdin_json = json.loads(mock_process.communicate.call_args[0][0])
        assert "body" in stdin_json
        assert stdin_json["body"]  # must be non-empty

    @pytest.mark.asyncio
    async def test_fallback_includes_root_cause_details(self, mocker) -> None:
        """Fallback comment includes collapsed <details> blocks for issues with root_cause."""
        report_with_root_cause = ReviewResult(
            project_key="test",
            branch="feat",
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
                            root_cause="<p>Unused variables clutter the code.</p>",
                        ),
                        ReviewIssue(
                            rule="python:S1481",
                            message="Remove another unused variable",
                            line=20,
                            severity="MAJOR",
                            root_cause="<p>Unused variables clutter the code.</p>",
                        ),
                        ReviewIssue(
                            rule="python:S1192",
                            message="No root cause here",
                            line=30,
                            severity="MINOR",
                        ),
                    ],
                ),
            ],
        )

        mock_fail = AsyncMock()
        mock_fail.communicate.return_value = (b'{"message": "Unprocessable Entity"}', b"")
        mock_fail.returncode = 1

        mock_success = AsyncMock()
        mock_success.communicate.return_value = (b"{}", b"")
        mock_success.returncode = 0

        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            side_effect=[mock_fail, mock_success],
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(42, report_with_root_cause)

        body = json.loads(mock_success.communicate.call_args[0][0])["body"]
        assert "<details>" in body
        assert "python:S1481 — why this matters" in body
        assert "<p>Unused variables clutter the code.</p>" in body
        # Deduplicated — only one <details> block for python:S1481
        assert body.count("<details>") == 1
        # Issue without root_cause produces no <details> block for its rule
        assert "python:S1192" in body
        assert "python:S1192 — why this matters" not in body

    @pytest.mark.asyncio
    async def test_fallback_logs_warning_with_inline_error(self, sample_report, mocker, caplog) -> None:
        """post_review logs a warning with the inline error reason before falling back."""
        import logging

        mock_fail = AsyncMock()
        mock_fail.communicate.return_value = (b"", b"validation error: line outside diff")
        mock_fail.returncode = 1

        mock_success = AsyncMock()
        mock_success.communicate.return_value = (b"{}", b"")
        mock_success.returncode = 0

        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            side_effect=[mock_fail, mock_success],
        )
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

    @pytest.mark.asyncio
    async def test_multiple_files_aggregated(self, mocker) -> None:
        """post_review aggregates comments from multiple files into one review."""
        multi_file_report = ReviewResult(
            project_key="test",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path="src/a.py",
                    issues=[ReviewIssue(rule="python:S1", message="Issue A1", line=1)],
                ),
                FileReview(
                    file_path="src/b.py",
                    issues=[
                        ReviewIssue(rule="python:S2", message="Issue B1", line=10),
                        ReviewIssue(rule="python:S3", message="Issue B2", line=20),
                    ],
                ),
            ],
        )

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0
        mocker.patch(
            "dotharness.vibe_heal.review.github.asyncio.create_subprocess_exec",
            return_value=mock_process,
        )
        mocker.patch(
            "dotharness.vibe_heal.review.github.run_command",
            new_callable=AsyncMock,
            return_value=mocker.MagicMock(success=True, stdout="myorg/myrepo"),
        )

        client = GitHubReviewClient()
        await client.post_review(5, multi_file_report)

        stdin_json = json.loads(mock_process.communicate.call_args[0][0])
        assert len(stdin_json["comments"]) == 3
        paths = {c["path"] for c in stdin_json["comments"]}
        assert "src/a.py" in paths
        assert "src/b.py" in paths


class TestBuildPayloadCoverage:
    def _make_report(self, **file_kwargs) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feature/x",
            base_branch="origin/main",
            files=[FileReview(file_path="src/f.py", **file_kwargs)],
        )

    def test_coverage_included_in_body_when_present(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(coverage_pct=72.0, covered_lines=18, instrumented_changed_lines=25)
        payload = client.build_payload(report)
        assert "**72.0%**" in payload["body"]  # 72.0 < 80, so bold
        assert "| 18 |" in payload["body"]
        assert "| 25 |" in payload["body"]
        assert "Coverage on changed lines" in payload["body"]

    def test_coverage_absent_from_body_when_none(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report()  # coverage_pct defaults to None
        payload = client.build_payload(report)
        assert "Coverage on changed lines" not in payload["body"]

    def test_coverage_included_in_fallback_body(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(coverage_pct=50.0, covered_lines=5, instrumented_changed_lines=10)
        payload = client._build_fallback_payload(report)
        assert "**50.0%**" in payload["body"]  # 50.0 < 80, so bold
        assert "| 5 |" in payload["body"]
        assert "| 10 |" in payload["body"]
        assert "Coverage on changed lines" in payload["body"]


class TestBuildPayloadProjectLabel:
    def _make_report(self, project_label: str = "", files: list[FileReview] | None = None) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            project_label=project_label,
            branch="feature/x",
            base_branch="origin/main",
            files=files or [],
        )

    def test_label_included_in_no_findings_summary(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(project_label="services/api")
        payload = client.build_payload(report)
        assert "SonarQube [services/api]: no findings on or near changed lines." in payload["body"]

    def test_label_included_in_findings_summary(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(
            project_label="services/api",
            files=[
                FileReview(
                    file_path="src/f.py",
                    issues=[ReviewIssue(rule="python:S1481", message="Remove unused variable", line=10)],
                )
            ],
        )
        payload = client.build_payload(report)
        assert "SonarQube [services/api]: 1 finding(s)" in payload["body"]

    def test_no_brackets_when_label_absent(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(project_label="")
        payload = client.build_payload(report)
        assert "SonarQube: no findings on or near changed lines." in payload["body"]
        assert "[" not in payload["body"].splitlines()[0]


class TestBuildPayloadHeadSha:
    def _make_report(self, head_sha: str = "", files: list[FileReview] | None = None) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feature/x",
            head_sha=head_sha,
            base_branch="origin/main",
            files=files or [],
        )

    def test_commit_included_in_no_findings_summary(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(head_sha="abcdef1234567890")
        payload = client.build_payload(report)
        assert "SonarQube: no findings on or near changed lines. (commit abcdef1)" in payload["body"]

    def test_commit_included_in_findings_summary(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(
            head_sha="abcdef1234567890",
            files=[
                FileReview(
                    file_path="src/f.py",
                    issues=[ReviewIssue(rule="python:S1481", message="Remove unused variable", line=10)],
                )
            ],
        )
        payload = client.build_payload(report)
        assert payload["body"].splitlines()[0].endswith("(commit abcdef1)")

    def test_no_commit_suffix_when_head_sha_absent(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(head_sha="")
        payload = client.build_payload(report)
        assert "SonarQube: no findings on or near changed lines." in payload["body"]
        assert "(commit" not in payload["body"]

    def test_commit_included_in_fallback_body(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report(
            head_sha="abcdef1234567890",
            files=[
                FileReview(
                    file_path="src/f.py",
                    issues=[ReviewIssue(rule="python:S1481", message="Remove unused variable", line=10)],
                )
            ],
        )
        payload = client._build_fallback_payload(report)
        assert "(commit abcdef1)" in payload["body"]


class TestBuildPayloadCap:
    def _make_report_with_n_issues(self, n: int) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path=f"src/file_{i}.py",
                    issues=[ReviewIssue(rule="python:S1", message=f"Issue {i}", line=i + 1, on_changed_line=True)],
                )
                for i in range(n)
            ],
        )

    def test_comments_capped_at_max(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report_with_n_issues(_MAX_INLINE_COMMENTS + 10)
        payload = client.build_payload(report)
        assert len(payload["comments"]) == _MAX_INLINE_COMMENTS

    def test_overflow_appears_in_body(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report_with_n_issues(_MAX_INLINE_COMMENTS + 5)
        payload = client.build_payload(report)
        assert "5 finding(s) over the" in payload["body"]
        assert "capped" in payload["body"]
        assert "src/file_50.py" in payload["body"]  # overflow entries are actually rendered, not just the header

    def test_summary_counts_overflow(self) -> None:
        total = _MAX_INLINE_COMMENTS + 3
        client = GitHubReviewClient()
        report = self._make_report_with_n_issues(total)
        payload = client.build_payload(report)
        assert f"{total} finding(s)" in payload["body"]

    def test_under_cap_posts_all_inline(self) -> None:
        client = GitHubReviewClient()
        report = self._make_report_with_n_issues(5)
        payload = client.build_payload(report)
        assert len(payload["comments"]) == 5
        assert "capped" not in payload["body"]
