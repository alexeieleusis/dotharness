"""Tests for ReviewOrchestrator class."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from dotharness.vibe_heal.config import VibeHealConfig
    from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

from dotharness.vibe_heal.git.diff_parser import DiffLines
from dotharness.vibe_heal.review.models import (
    FileDiagnostics,
    FileReview,
    ReviewIssue,
    ReviewResult,
)
from dotharness.vibe_heal.review.reporter import load_report, write_reports
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisResult
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


@pytest.fixture
def config() -> VibeHealConfig:
    """Create test configuration."""
    from dotharness.vibe_heal.config import VibeHealConfig

    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="test-token",
        sonarqube_project_key="my-project",
    )


def _make_sonar_issue(line: int, file_path: str = "src/file.py") -> SonarQubeIssue:
    """Create a SonarQube issue for testing."""
    return SonarQubeIssue(
        key=f"issue-{line}",
        rule="python:S1481",
        message=f"Issue at line {line}",
        component=file_path,
        line=line,
        severity="MAJOR",
    )


def _make_review_result(total_issues: int = 2) -> ReviewResult:
    """Create a sample ReviewResult for testing."""
    return ReviewResult(
        project_key="temp-project-key",
        branch="feature/test",
        base_branch="origin/main",
        generated_at=datetime(2025, 10, 24, 14, 30, 0, tzinfo=timezone.utc),
        files=[
            FileReview(
                file_path="src/file.py",
                issues=[
                    ReviewIssue(rule="python:S1481", message="Issue 1", line=10, severity="MAJOR"),
                    ReviewIssue(rule="python:S1192", message="Issue 2", line=20, severity="MINOR"),
                ],
            )
        ],
    )


@contextmanager
def _basic_analysis_patches(orchestrator, modified_files=None):
    """Context manager providing common patches for run_analysis tests.

    Provides the standard branch_analyzer and project_manager mocks that
    every run_analysis test needs. Tests can override individual patches
    by nesting additional patch.object() calls within the same with block.
    """
    if modified_files is None:
        modified_files = [Path("src/file.py")]

    temp_project = MagicMock()
    temp_project.project_key = "temp-key"
    temp_project.project_name = "temp-name"

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=modified_files,
            )
        )
        stack.enter_context(
            patch.object(
                orchestrator.branch_analyzer,
                "get_current_branch",
                return_value="feature/test",
            )
        )
        stack.enter_context(
            patch.object(
                orchestrator.branch_analyzer,
                "get_user_email",
                return_value="user@example.com",
            )
        )
        stack.enter_context(
            patch.object(
                orchestrator.project_manager,
                "create_temp_project",
                return_value=temp_project,
            )
        )
        yield


class TestReviewOrchestratorInit:
    """Tests for ReviewOrchestrator initialization."""

    def test_init_creates_sub_components(self, config: VibeHealConfig) -> None:
        """Test that the orchestrator creates all required sub-components."""
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        mock_client = AsyncMock()
        mock_analyzer = MagicMock()
        mock_parser = MagicMock()
        orchestrator = ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

        assert orchestrator.config == config
        assert orchestrator.client == mock_client
        assert orchestrator.branch_analyzer is mock_analyzer
        assert orchestrator.diff_parser is mock_parser
        assert orchestrator.project_manager is not None
        assert orchestrator.analysis_runner is not None
        assert orchestrator.github_client is not None


class TestResolveBaseBranch:
    """Tests for ReviewOrchestrator._resolve_base_branch()."""

    @pytest.fixture
    def orchestrator(self, config: VibeHealConfig):
        """Create ReviewOrchestrator with mocked git dependencies."""
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        mock_client = AsyncMock()
        mock_analyzer = MagicMock()
        mock_parser = MagicMock()
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

    @pytest.mark.asyncio
    async def test_explicit_base_branch_skips_detection(self, orchestrator) -> None:
        """An explicit base_branch is returned as-is; gh is never called."""
        with patch.object(
            orchestrator.github_client,
            "detect_pr_base_branch",
            new_callable=AsyncMock,
        ) as mock_detect:
            result = await orchestrator._resolve_base_branch("origin/develop", verbose=False)

        assert result == "origin/develop"
        mock_detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_detects_when_omitted(self, orchestrator) -> None:
        """When base_branch is None, the detected PR base is used with an origin/ prefix."""
        with patch.object(
            orchestrator.github_client,
            "detect_pr_base_branch",
            new_callable=AsyncMock,
            return_value="feature/lower-in-stack",
        ):
            result = await orchestrator._resolve_base_branch(None, verbose=False)

        assert result == "origin/feature/lower-in-stack"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_no_pr_found(self, orchestrator) -> None:
        """When detection returns None (no PR / gh unavailable), fall back to origin/main."""
        with patch.object(
            orchestrator.github_client,
            "detect_pr_base_branch",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await orchestrator._resolve_base_branch(None, verbose=False)

        assert result == "origin/main"

    @pytest.mark.asyncio
    async def test_detected_main_resolves_like_fallback(self, orchestrator) -> None:
        """A direct (non-stacked) PR targeting main resolves to the same value as the fallback."""
        with patch.object(
            orchestrator.github_client,
            "detect_pr_base_branch",
            new_callable=AsyncMock,
            return_value="main",
        ):
            result = await orchestrator._resolve_base_branch(None, verbose=False)

        assert result == "origin/main"


class TestRunAnalysis:
    """Tests for ReviewOrchestrator.run_analysis()."""

    @pytest.fixture
    def mock_branch_analyzer(self) -> MagicMock:
        """Create a mocked BranchAnalyzer for tests."""
        mock = MagicMock()
        mock.get_current_branch.return_value = "feature/test"
        mock.get_user_email.return_value = "user@example.com"
        mock.get_head_sha.return_value = "deadbeef" * 5
        return mock

    @pytest.fixture
    def mock_diff_parser(self) -> MagicMock:
        """Create a mocked DiffParser for tests."""
        mock = MagicMock()
        mock.get_diff_lines.return_value = DiffLines(new_lines={}, old_lines={})
        mock.get_raw_diff.return_value = ""
        return mock

    @pytest.fixture
    def orchestrator(self, config: VibeHealConfig, mock_branch_analyzer, mock_diff_parser):
        """Create ReviewOrchestrator with mocked git dependencies."""
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        mock_client = AsyncMock()
        return ReviewOrchestrator(config, mock_client, mock_branch_analyzer, mock_diff_parser)

    @pytest.mark.asyncio
    async def test_no_modified_files_returns_empty_result(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """When branch has no modified files, return empty result without creating temp project."""
        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[],
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project",
                new_callable=AsyncMock,
            ) as mock_create,
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                new_callable=AsyncMock,
            ) as mock_run,
        ):
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
            )

        assert result.success is True
        assert result.total_issues == 0
        assert result.files == []
        # Should not create temp project
        mock_create.assert_not_called()
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_base_branch_none_resolves_via_github_client(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """When base_branch is None, run_analysis resolves it via GitHubReviewClient
        before doing anything else, and the resolved value ends up on the result."""
        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[],
            ),
            patch.object(
                orchestrator.github_client,
                "detect_pr_base_branch",
                new_callable=AsyncMock,
                return_value="feature/lower-in-stack",
            ) as mock_detect,
        ):
            result = await orchestrator.run_analysis(
                base_branch=None,
                report_file=tmp_path / "review.json",
            )

        mock_detect.assert_called_once()
        assert result.base_branch == "origin/feature/lower-in-stack"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_analysis_failure_returns_error(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """When analysis fails, return error result and cleanup temp project."""
        with (
            _basic_analysis_patches(orchestrator),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(
                    success=False,
                    error_message="Scanner not found",
                ),
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
            )

        assert result.success is False
        assert "Scanner not found" in (result.error_message or "")
        mock_delete.assert_called_once_with("temp-key")

    @pytest.mark.asyncio
    async def test_no_issues_on_changed_lines(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """When there are no issues on changed lines, return empty result."""
        with (
            _basic_analysis_patches(orchestrator),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=([], 0),
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(success=True, task_id="task-1", dashboard_url="http://dash"),
            ),
            patch.object(
                orchestrator.client,
                "get_issues",
                return_value=[],
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
            )

        assert result.success is True
        assert result.total_issues == 0
        mock_delete.assert_called_once_with("temp-key")

    @pytest.mark.asyncio
    async def test_project_label_reflects_current_run_temp_project(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """project_label must come from this run's own temp project, not from
        whatever sonar-project.properties happens to contain on disk (which
        may be left over from a different, unrelated PR's interrupted run)."""
        with (
            _basic_analysis_patches(orchestrator),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=([], 0),
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(success=True, task_id="task-1", dashboard_url="http://dash"),
            ),
            patch.object(
                orchestrator.client,
                "get_issues",
                return_value=[],
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ),
        ):
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
            )

        assert result.project_label == "temp-name"

    @pytest.mark.asyncio
    async def test_full_happy_path(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """Full happy path: modified files, analysis succeeds, issues found on changed lines."""
        sonar_issues = [
            _make_sonar_issue(10),
            _make_sonar_issue(50),
            _make_sonar_issue(20),
        ]

        with (
            _basic_analysis_patches(orchestrator),
            patch.object(
                orchestrator.diff_parser,
                "get_diff_lines",
                return_value=DiffLines(new_lines={"src/file.py": {10, 20}}, old_lines={}),
            ),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=([], 0),
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(success=True, task_id="task-1", dashboard_url="http://dash"),
            ),
            patch.object(
                orchestrator.client,
                "get_issues",
                return_value=sonar_issues,
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
            patch.object(orchestrator, "_get_active_duplications", new_callable=AsyncMock, return_value=[]),
            patch.object(orchestrator, "_get_resolved_duplications", new_callable=AsyncMock, return_value=[]),
            patch.object(orchestrator.client, "get_rule_details", AsyncMock(return_value=None)),
        ):
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
            )

        assert result.success is True
        assert result.total_issues == 2
        assert len(result.files) == 1
        assert result.files[0].file_path == "src/file.py"
        assert len(result.files[0].issues) == 2
        issue_lines = {i.line for i in result.files[0].issues}
        assert issue_lines == {10, 20}
        mock_delete.assert_called_once_with("temp-key")
        assert len(result.diagnostics) == 1
        diag = result.diagnostics[0]
        assert diag.file_path == "src/file.py"
        assert diag.changed_lines == sorted({10, 20})
        assert diag.sonar_issue_count == 3
        assert diag.sonar_issue_lines == [10, 20, 50]

    @pytest.mark.asyncio
    async def test_file_patterns_filter_files(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """File patterns should filter which files are analyzed."""
        with (
            _basic_analysis_patches(
                orchestrator,
                modified_files=[Path("src/file.py"), Path("docs/readme.md")],
            ),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=([], 0),
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(success=True, task_id="task-1", dashboard_url="http://dash"),
            ),
            patch.object(
                orchestrator.client,
                "get_issues",
                return_value=[],
            ) as mock_get_issues,
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ),
        ):
            await orchestrator.run_analysis(
                base_branch="origin/main",
                file_patterns=["*.py"],
                report_file=tmp_path / "review.json",
            )

        mock_get_issues.assert_called_once()
        component = mock_get_issues.call_args.kwargs.get("component", "")
        assert "src/file.py" in component

    @pytest.mark.asyncio
    async def test_temp_project_cleanup_on_exception(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """Temp project is cleaned up even when an unexpected exception occurs."""
        with (
            _basic_analysis_patches(orchestrator),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                side_effect=RuntimeError("Unexpected error"),
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
            )

        assert result.success is False
        assert "Unexpected error" in (result.error_message or "")
        mock_delete.assert_called_once_with("temp-key")

    @pytest.mark.asyncio
    async def test_exception_mid_loop_preserves_accumulated_result(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """An exception partway through the per-file loop must not discard
        diagnostics/files/branch/head_sha already accumulated by earlier files.

        Regression guard for the in-place-mutation refactor: reintroducing
        `return ReviewAnalysisResult(...)` in the except branch would reset
        every field asserted below to blank while still passing every other
        test in this class, since those all fail before the loop starts.
        """
        first_diag = FileDiagnostics(file_path="src/file1.py", lookup_key="src/file1.py")
        first_issue = ReviewIssue(rule="python:S1481", message="Issue 1", line=10, severity="MAJOR")

        with (
            _basic_analysis_patches(orchestrator, modified_files=[Path("src/file1.py"), Path("src/file2.py")]),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=([], 0, 0),
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(success=True, task_id="task-1", dashboard_url="http://dash"),
            ),
            patch.object(
                orchestrator,
                "_get_filtered_issues",
                side_effect=[([first_issue], first_diag), RuntimeError("boom on second file")],
            ),
            patch.object(orchestrator, "_enrich_issues_with_descriptions", new_callable=AsyncMock),
            patch.object(orchestrator, "_get_active_duplications", new_callable=AsyncMock, return_value=[]),
            patch.object(orchestrator, "_get_resolved_duplications", new_callable=AsyncMock, return_value=[]),
            patch.object(orchestrator, "_fetch_coverage", new_callable=AsyncMock, return_value=(None, 0, 0)),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
            )

        assert result.success is False
        assert "boom on second file" in (result.error_message or "")
        # These were set before the loop started and must survive the exception.
        assert result.branch == "feature/test"
        assert result.head_sha == "deadbeef" * 5
        # These were accumulated by the first file before the second one raised.
        assert result.diagnostics == [first_diag]
        assert len(result.files) == 1
        assert result.files[0].file_path == "src/file1.py"
        assert result.report_file is None
        mock_delete.assert_called_once_with("temp-key")

    @pytest.mark.asyncio
    async def test_writes_report_file(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """Successful analysis writes a valid report file that can be loaded."""
        temp_project = MagicMock()
        temp_project.project_key = "temp-key"
        temp_project.project_name = "temp-name"

        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[Path("src/file.py")],
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_current_branch",
                return_value="feature/test",
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_user_email",
                return_value="user@example.com",
            ),
            patch.object(
                orchestrator.diff_parser,
                "get_diff_lines",
                return_value=DiffLines(new_lines={"src/file.py": {10}}, old_lines={}),
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project",
                return_value=temp_project,
            ),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=([], 0),
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(success=True, task_id="task-1", dashboard_url="http://dash"),
            ),
            patch.object(
                orchestrator.client,
                "get_issues",
                return_value=[_make_sonar_issue(10)],
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ),
            patch.object(orchestrator, "_get_active_duplications", new_callable=AsyncMock, return_value=[]),
            patch.object(orchestrator, "_get_resolved_duplications", new_callable=AsyncMock, return_value=[]),
            patch.object(orchestrator.client, "get_rule_details", AsyncMock(return_value=None)),
        ):
            report_path = tmp_path / "reports" / "review.json"
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=report_path,
            )

        assert result.success is True
        # Verify report was written and is loadable
        report_dir = report_path.parent
        assert (report_dir / "review.json").exists()
        loaded = load_report(report_dir)
        assert loaded.total_issues == 1
        assert loaded.branch == "feature/test"
        assert loaded.base_branch == "origin/main"
        # Diagnostics must be persisted in the JSON report
        assert len(loaded.diagnostics) == 1
        diag = loaded.diagnostics[0]
        assert diag.sonar_issue_count == 1
        assert diag.sonar_issue_lines == [10]
        assert diag.changed_lines == [10]


class TestRunPost:
    """Tests for ReviewOrchestrator.run_post()."""

    @pytest.fixture
    def orchestrator(self, config: VibeHealConfig):
        """Create ReviewOrchestrator with mocked dependencies."""
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        mock_client = AsyncMock()
        mock_analyzer = MagicMock()
        mock_parser = MagicMock()
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

    @pytest.mark.asyncio
    async def test_reads_saved_report_and_posts_review(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """run_post loads the saved report and posts review comments."""
        # Write a report to disk
        result = _make_review_result()
        report_dir = tmp_path / "reports"
        write_reports(result, report_dir)
        report_file = report_dir / "review.json"

        with (
            patch.object(
                orchestrator.github_client,
                "detect_pr",
                return_value=42,
            ) as mock_detect,
            patch.object(
                orchestrator.github_client,
                "post_review",
                new_callable=AsyncMock,
            ) as mock_post,
        ):
            await orchestrator.run_post(
                report_file=report_file,
                pr_number=None,
            )

        mock_detect.assert_called_once()
        mock_post.assert_called_once()
        # Verify the posted report matches what was saved
        posted_report = mock_post.call_args[0][1]
        assert posted_report.total_issues == 2
        assert posted_report.branch == "feature/test"

    @pytest.mark.asyncio
    async def test_explicit_pr_number_skips_detection(
        self,
        orchestrator,
        tmp_path: Path,
    ) -> None:
        """When pr_number is provided, detect_pr is not called."""
        result = _make_review_result()
        report_dir = tmp_path / "reports"
        write_reports(result, report_dir)
        report_file = report_dir / "review.json"

        with (
            patch.object(
                orchestrator.github_client,
                "detect_pr",
                return_value=42,
            ) as mock_detect,
            patch.object(
                orchestrator.github_client,
                "post_review",
                new_callable=AsyncMock,
            ) as mock_post,
        ):
            await orchestrator.run_post(
                report_file=report_file,
                pr_number=99,
            )

        mock_detect.assert_not_called()
        # post_review should be called with explicit PR number
        assert mock_post.call_count == 1
        posted_pr = mock_post.call_args[0][0]
        assert posted_pr == 99

    @pytest.mark.asyncio
    async def test_report_file_not_found_raises(self, orchestrator) -> None:
        """When the report file doesn't exist, raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            await orchestrator.run_post(
                report_file=Path("/nonexistent/path/review.json"),
            )


class TestToRepoRelative:
    """Tests for ReviewOrchestrator._to_repo_relative()."""

    @pytest.fixture
    def orchestrator(self, config: VibeHealConfig) -> ReviewOrchestrator:
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        mock_client = AsyncMock()
        mock_analyzer = MagicMock()
        mock_analyzer.repo.working_dir = "/repo"
        mock_parser = MagicMock()
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

    def test_absolute_path_made_repo_relative(self, orchestrator) -> None:
        result = orchestrator._to_repo_relative(Path("/repo/src/file.py"))
        assert result == "src/file.py"

    def test_repo_root_relative_path_when_running_from_root(self, orchestrator) -> None:
        with patch("pathlib.Path.cwd", return_value=Path("/repo")):
            result = orchestrator._to_repo_relative(Path("src/file.py"))
        assert result == "src/file.py"

    def test_cwd_relative_path_from_subdirectory(self, orchestrator) -> None:
        """CWD-relative path from BranchAnalyzer when running from a subdirectory is
        resolved to repo-root-relative so DiffParser map lookups succeed."""
        with patch("pathlib.Path.cwd", return_value=Path("/repo/packages/app")):
            result = orchestrator._to_repo_relative(Path("src/file.py"))
        assert result == "packages/app/src/file.py"


class TestGetActiveDuplications:
    """Tests for ReviewOrchestrator._get_active_duplications()."""

    @pytest.fixture
    def orchestrator(self) -> ReviewOrchestrator:
        from dotharness.vibe_heal.config import VibeHealConfig
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        config = VibeHealConfig(
            sonarqube_url="https://sonar.test.com",
            sonarqube_token="test-token",
            sonarqube_project_key="temp-project",
        )
        mock_client = AsyncMock()
        mock_analyzer = MagicMock()
        mock_analyzer.repo.working_dir = "/repo"
        mock_parser = MagicMock()
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

    def _make_response(self, from_line: int, size: int, other_file: str = "src/other.py") -> MagicMock:
        """Build a mock DuplicationsResponse with one group."""
        from dotharness.vibe_heal.deduplication.models import DuplicationBlock, DuplicationGroup, DuplicationsResponse

        target_block = DuplicationBlock(**{"from": from_line, "size": size, "_ref": "1"})
        other_block = DuplicationBlock(**{"from": 50, "size": 10, "_ref": "2"})
        group = DuplicationGroup(blocks=[target_block, other_block])

        file_info_target = MagicMock()
        file_info_target.key = "temp-project:src/file.py"

        file_info_other = MagicMock()
        file_info_other.key = f"temp-project:{other_file}"

        response = MagicMock(spec=DuplicationsResponse)
        response.duplications = [group]
        response.get_target_file_ref.return_value = "1"
        response.get_file_info.side_effect = lambda ref: file_info_target if ref == "1" else file_info_other
        return response

    def _make_diag(self, file_path: str = "src/file.py") -> FileDiagnostics:
        return FileDiagnostics(file_path=file_path, lookup_key=file_path)

    @pytest.mark.asyncio
    async def test_block_intersecting_changed_lines_is_reported(self, orchestrator) -> None:
        """A duplication block overlapping changed lines produces a ReviewDuplication."""
        response = self._make_response(from_line=10, size=15)  # block lines 10-24

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.return_value = response

        with patch("dotharness.vibe_heal.review.orchestrator.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            diag = self._make_diag()
            result = await orchestrator._get_active_duplications(
                Path("src/file.py"),
                {"src/file.py": {12, 15}},  # lines inside the block
                diag,
                project_key="temp-project",
            )

        assert len(result) == 1
        assert result[0].from_line == 10
        assert result[0].to_line == 24
        assert len(result[0].other_locations) == 1
        assert result[0].other_locations[0].file_path == "src/other.py"
        assert diag.active_dup_api_status == "ok"
        assert diag.active_dup_groups_found == 1
        assert diag.active_dup_target_ref_found is True
        assert diag.active_dup_blocks_intersecting == 1

    @pytest.mark.asyncio
    async def test_non_intersecting_block_is_skipped(self, orchestrator) -> None:
        """A duplication block not overlapping changed lines produces nothing."""
        response = self._make_response(from_line=100, size=10)  # block lines 100-109

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.return_value = response

        with patch("dotharness.vibe_heal.review.orchestrator.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            diag = self._make_diag()
            result = await orchestrator._get_active_duplications(
                Path("src/file.py"),
                {"src/file.py": {5, 6, 7}},  # lines outside the block
                diag,
                project_key="temp-project",
            )

        assert result == []
        assert diag.active_dup_api_status == "ok"
        assert diag.active_dup_groups_found == 1
        assert diag.active_dup_blocks_intersecting == 0

    @pytest.mark.asyncio
    async def test_no_changed_lines_returns_empty(self, orchestrator) -> None:
        """When file has no changed lines, returns empty without calling API."""
        with patch("dotharness.vibe_heal.review.orchestrator.DuplicationClient") as MockDupClient:
            diag = self._make_diag()
            result = await orchestrator._get_active_duplications(
                Path("src/file.py"),
                {},  # no entry for this file
                diag,
                project_key="temp-project",
            )

        MockDupClient.assert_not_called()
        assert result == []
        assert diag.active_dup_api_status == "skipped_no_changed_lines"

    @pytest.mark.asyncio
    async def test_component_not_found_returns_empty(self, orchestrator) -> None:
        """ComponentNotFoundError from the API is recorded in diagnostics."""
        from dotharness.vibe_heal.sonarqube.exceptions import ComponentNotFoundError

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.side_effect = ComponentNotFoundError("not found")

        with patch("dotharness.vibe_heal.review.orchestrator.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            diag = self._make_diag()
            result = await orchestrator._get_active_duplications(
                Path("src/file.py"),
                {"src/file.py": {10}},
                diag,
                project_key="temp-project",
            )

        assert result == []
        assert diag.active_dup_api_status == "component_not_found"

    @pytest.mark.asyncio
    async def test_unexpected_exception_recorded_in_diagnostics(self, orchestrator) -> None:
        """Any unexpected exception is recorded in diagnostics rather than propagating."""
        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.side_effect = ValueError("bad response")

        with patch("dotharness.vibe_heal.review.orchestrator.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            diag = self._make_diag()
            result = await orchestrator._get_active_duplications(
                Path("src/file.py"),
                {"src/file.py": {10}},
                diag,
                project_key="temp-project",
            )

        assert result == []
        assert diag.active_dup_api_status.startswith("error:ValueError:")


class TestGetResolvedDuplications:
    """Tests for ReviewOrchestrator._get_resolved_duplications()."""

    @pytest.fixture
    def orchestrator(self) -> ReviewOrchestrator:
        from dotharness.vibe_heal.config import VibeHealConfig
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        config = VibeHealConfig(
            sonarqube_url="https://sonar.test.com",
            sonarqube_token="test-token",
            sonarqube_project_key="temp-project",
        )
        mock_client = AsyncMock()
        mock_client.config = config
        mock_analyzer = MagicMock()
        mock_analyzer.repo.working_dir = "/repo"
        mock_parser = MagicMock()
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

    def _make_diag(self, file_path: str = "src/file.py") -> FileDiagnostics:
        return FileDiagnostics(file_path=file_path, lookup_key=file_path)

    def _make_response(self, from_line: int, size: int) -> MagicMock:
        from dotharness.vibe_heal.deduplication.models import DuplicationBlock, DuplicationGroup, DuplicationsResponse

        target_block = DuplicationBlock(**{"from": from_line, "size": size, "_ref": "1"})
        other_block = DuplicationBlock(**{"from": 100, "size": 10, "_ref": "2"})
        group = DuplicationGroup(blocks=[target_block, other_block])

        file_info_other = MagicMock()
        file_info_other.key = "main-project:src/old.py"

        response = MagicMock(spec=DuplicationsResponse)
        response.duplications = [group]
        response.get_target_file_ref.return_value = "1"
        response.get_file_info.return_value = file_info_other
        return response

    @pytest.mark.asyncio
    async def test_resolved_duplication_detected(self, orchestrator) -> None:
        """A main-project block overlapping old changed lines and not in active ranges produces a warning."""
        response = self._make_response(from_line=45, size=16)  # main block lines 45-60

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.return_value = response

        with patch("dotharness.vibe_heal.review.duplication_scope.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            diag = self._make_diag()
            result = await orchestrator._get_resolved_duplications(
                Path("src/file.py"),
                changed_lines_map={"src/file.py": {50}},  # new changed lines (anchor)
                old_changed_lines_map={"src/file.py": {50}},  # old lines inside the block
                active_dup_ranges=set(),  # no active dups
                original_project_key="main-project",
                diag=diag,
            )

        assert len(result) == 1
        assert result[0].main_from_line == 45
        assert result[0].main_to_line == 60
        assert result[0].anchor_new_line == 50
        assert result[0].other_locations[0].file_path == "src/old.py"
        assert diag.resolved_dup_api_status == "ok"
        assert diag.resolved_dup_groups_found == 1

    @pytest.mark.asyncio
    async def test_skipped_when_covered_by_active_dup(self, orchestrator) -> None:
        """A resolved duplication block is skipped if Feature 1 already found it active."""
        response = self._make_response(from_line=45, size=16)

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.return_value = response

        with patch("dotharness.vibe_heal.review.duplication_scope.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            diag = self._make_diag()
            result = await orchestrator._get_resolved_duplications(
                Path("src/file.py"),
                changed_lines_map={"src/file.py": {50}},
                old_changed_lines_map={"src/file.py": {50}},
                active_dup_ranges={(45, 60)},  # covered by Feature 1
                original_project_key="main-project",
                diag=diag,
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_no_old_changed_lines_returns_empty(self, orchestrator) -> None:
        """When file has no old changed lines, returns empty without API call."""
        with patch("dotharness.vibe_heal.review.duplication_scope.DuplicationClient") as MockDupClient:
            diag = self._make_diag()
            result = await orchestrator._get_resolved_duplications(
                Path("src/file.py"),
                changed_lines_map={},
                old_changed_lines_map={},
                active_dup_ranges=set(),
                original_project_key="main-project",
                diag=diag,
            )

        MockDupClient.assert_not_called()
        assert result == []
        assert diag.resolved_dup_api_status == "skipped_no_changed_lines"

    @pytest.mark.asyncio
    async def test_silent_skip_on_component_not_found(self, orchestrator) -> None:
        """ComponentNotFoundError on main project query is recorded in diagnostics."""
        from dotharness.vibe_heal.sonarqube.exceptions import ComponentNotFoundError

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.side_effect = ComponentNotFoundError("not found")

        with patch("dotharness.vibe_heal.review.duplication_scope.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            diag = self._make_diag()
            result = await orchestrator._get_resolved_duplications(
                Path("src/file.py"),
                changed_lines_map={"src/file.py": {50}},
                old_changed_lines_map={"src/file.py": {50}},
                active_dup_ranges=set(),
                original_project_key="main-project",
                diag=diag,
            )

        assert result == []
        assert diag.resolved_dup_api_status == "component_not_found"

    @pytest.mark.asyncio
    async def test_config_not_mutated_during_query(self, orchestrator) -> None:
        """DuplicationClient is constructed with original_project_key; orchestrator config is never mutated."""
        orchestrator.config.sonarqube_project_key = "temp-project"

        response = MagicMock()
        response.duplications = []

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.return_value = response

        with patch("dotharness.vibe_heal.review.duplication_scope.DuplicationClient") as MockDupClient:
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            await orchestrator._get_resolved_duplications(
                Path("src/file.py"),
                changed_lines_map={"src/file.py": {50}},
                old_changed_lines_map={"src/file.py": {50}},
                active_dup_ranges=set(),
                original_project_key="main-project",
                diag=self._make_diag(),
            )

        # Config copy passed to DuplicationClient must use the main project key
        passed_config = MockDupClient.call_args[0][0]
        assert passed_config.sonarqube_project_key == "main-project"
        # Orchestrator's own config must be untouched
        assert orchestrator.config.sonarqube_project_key == "temp-project"


class TestSameFileDuplication:
    """Tests for same-file duplication behavior in both active and resolved flows."""

    @pytest.fixture
    def orchestrator(self) -> ReviewOrchestrator:
        from dotharness.vibe_heal.config import VibeHealConfig
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        config = VibeHealConfig(
            sonarqube_url="https://sonar.test.com",
            sonarqube_token="test-token",
            sonarqube_project_key="temp-project",
        )
        mock_client = AsyncMock()
        mock_client.config = config
        mock_analyzer = MagicMock()
        mock_analyzer.repo.working_dir = "/repo"
        mock_parser = MagicMock()
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_id,project_key,file_key_prefix,expected_from_attr,expected_to_attr",
        [
            pytest.param(
                "active",
                "temp-project",
                "temp-project:src/file.py",
                "from_line",
                "to_line",
                id="active",
            ),
            pytest.param(
                "resolved",
                "main-project",
                "main-project:src/file.py",
                "main_from_line",
                "main_to_line",
                id="resolved",
            ),
        ],
    )
    async def test_same_file_duplication_includes_other_locations(
        self,
        orchestrator,
        case_id: str,
        project_key: str,
        file_key_prefix: str,
        expected_from_attr: str,
        expected_to_attr: str,
    ) -> None:
        """When both duplication blocks are in the same file (same _ref), the other
        block should still appear in other_locations for both active and resolved flows."""
        from dotharness.vibe_heal.deduplication.models import DuplicationBlock, DuplicationGroup, DuplicationsResponse

        target_block = DuplicationBlock(**{"from": 171, "size": 35, "_ref": "1"})
        other_block = DuplicationBlock(**{"from": 249, "size": 35, "_ref": "1"})
        group = DuplicationGroup(blocks=[target_block, other_block])

        file_info = MagicMock()
        file_info.key = file_key_prefix

        response = MagicMock(spec=DuplicationsResponse)
        response.duplications = [group]
        response.get_target_file_ref.return_value = "1"
        response.get_file_info.return_value = file_info

        mock_dup_instance = AsyncMock()
        mock_dup_instance.get_duplications_for_file.return_value = response

        diag = FileDiagnostics(file_path="src/file.py", lookup_key="src/file.py")

        with (
            patch("dotharness.vibe_heal.review.orchestrator.DuplicationClient") as MockDupClient,
            patch("dotharness.vibe_heal.review.duplication_scope.DuplicationClient", MockDupClient),
        ):
            MockDupClient.return_value.__aenter__ = AsyncMock(return_value=mock_dup_instance)
            MockDupClient.return_value.__aexit__ = AsyncMock(return_value=None)

            if case_id == "active":
                result = await orchestrator._get_active_duplications(
                    Path("src/file.py"),
                    {"src/file.py": {180, 190}},
                    diag,
                    project_key=project_key,
                )
            else:
                result = await orchestrator._get_resolved_duplications(
                    Path("src/file.py"),
                    changed_lines_map={"src/file.py": {180}},
                    old_changed_lines_map={"src/file.py": {180}},
                    active_dup_ranges=set(),
                    original_project_key=project_key,
                    diag=diag,
                )

        assert len(result) == 1
        assert getattr(result[0], expected_from_attr) == 171
        assert getattr(result[0], expected_to_attr) == 205
        assert len(result[0].other_locations) == 1
        assert result[0].other_locations[0].file_path == "src/file.py"
        assert result[0].other_locations[0].from_line == 249
        assert result[0].other_locations[0].to_line == 283


class TestRunAnalysisCoverage:
    """Tests for the --coverage flag in run_analysis."""

    @pytest.fixture
    def mock_diff_parser(self) -> MagicMock:
        mock = MagicMock()
        # Default: "src/file.py" has changed lines {10, 20} so changed_lines_for_file is non-empty
        mock.get_diff_lines.return_value = DiffLines(new_lines={"src/file.py": {10, 20}}, old_lines={})
        mock.get_raw_diff.return_value = ""
        return mock

    @pytest.fixture
    def orchestrator(self, config: VibeHealConfig, mock_diff_parser: MagicMock) -> ReviewOrchestrator:
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        mock_client = AsyncMock()
        mock_analyzer = MagicMock()
        mock_analyzer.get_head_sha.return_value = "deadbeef" * 5
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_diff_parser)

    def _standard_patches(self, orchestrator):
        """All patches needed to reach the per-file loop."""
        return (
            _basic_analysis_patches(orchestrator),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=([], 0),
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(success=True, task_id="t1", dashboard_url="http://d"),
            ),
            patch.object(orchestrator.project_manager, "delete_project", new_callable=AsyncMock),
        )

    @pytest.mark.asyncio
    async def test_coverage_false_does_not_call_get_line_coverage(self, orchestrator, tmp_path: Path) -> None:
        mock_glc = AsyncMock(return_value=(5, 10))
        with ExitStack() as stack:
            for ctx in self._standard_patches(orchestrator):
                stack.enter_context(ctx)
            stack.enter_context(patch.object(orchestrator.client, "get_issues", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator, "_get_active_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator, "_get_resolved_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator.client, "get_line_coverage", mock_glc))
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
                coverage=False,
            )
        assert result.success is True
        mock_glc.assert_not_called()

    @pytest.mark.asyncio
    async def test_coverage_true_populates_coverage_pct(self, orchestrator, tmp_path: Path) -> None:
        with ExitStack() as stack:
            for ctx in self._standard_patches(orchestrator):
                stack.enter_context(ctx)
            stack.enter_context(
                patch.object(orchestrator.client, "get_issues", AsyncMock(return_value=[_make_sonar_issue(10)]))
            )
            stack.enter_context(patch.object(orchestrator, "_get_active_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator, "_get_resolved_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator.client, "get_line_coverage", AsyncMock(return_value=(8, 10))))
            stack.enter_context(patch.object(orchestrator.client, "get_rule_details", AsyncMock(return_value=None)))
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
                coverage=True,
            )
        assert result.success is True
        assert len(result.files) == 1
        fr = result.files[0]
        assert fr.coverage_pct == 80.0
        assert fr.covered_lines == 8
        assert fr.instrumented_changed_lines == 10

    @pytest.mark.asyncio
    async def test_coverage_true_none_result_keeps_file_when_issues_exist(self, orchestrator, tmp_path: Path) -> None:
        with ExitStack() as stack:
            for ctx in self._standard_patches(orchestrator):
                stack.enter_context(ctx)
            stack.enter_context(
                patch.object(orchestrator.client, "get_issues", AsyncMock(return_value=[_make_sonar_issue(10)]))
            )
            stack.enter_context(patch.object(orchestrator, "_get_active_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator, "_get_resolved_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator.client, "get_line_coverage", AsyncMock(return_value=None)))
            stack.enter_context(patch.object(orchestrator.client, "get_rule_details", AsyncMock(return_value=None)))
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
                coverage=True,
            )
        assert result.success is True
        assert len(result.files) == 1
        assert result.files[0].coverage_pct is None

    @pytest.mark.asyncio
    async def test_coverage_exception_is_caught_processing_continues(self, orchestrator, tmp_path: Path) -> None:
        with ExitStack() as stack:
            for ctx in self._standard_patches(orchestrator):
                stack.enter_context(ctx)
            stack.enter_context(
                patch.object(orchestrator.client, "get_issues", AsyncMock(return_value=[_make_sonar_issue(10)]))
            )
            stack.enter_context(patch.object(orchestrator, "_get_active_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator, "_get_resolved_duplications", AsyncMock(return_value=[])))
            stack.enter_context(
                patch.object(
                    orchestrator.client, "get_line_coverage", AsyncMock(side_effect=RuntimeError("network error"))
                )
            )
            stack.enter_context(patch.object(orchestrator.client, "get_rule_details", AsyncMock(return_value=None)))
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
                coverage=True,
            )
        assert result.success is True
        assert result.files[0].coverage_pct is None

    @pytest.mark.asyncio
    async def test_coverage_only_file_included_when_coverage_pct_not_none(self, orchestrator, tmp_path: Path) -> None:
        """A file with no issues/dups but coverage data IS included in result.files."""
        with ExitStack() as stack:
            for ctx in self._standard_patches(orchestrator):
                stack.enter_context(ctx)
            stack.enter_context(patch.object(orchestrator.client, "get_issues", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator, "_get_active_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator, "_get_resolved_duplications", AsyncMock(return_value=[])))
            stack.enter_context(patch.object(orchestrator.client, "get_line_coverage", AsyncMock(return_value=(3, 5))))
            result = await orchestrator.run_analysis(
                base_branch="origin/main",
                report_file=tmp_path / "review.json",
                coverage=True,
            )
        assert len(result.files) == 1
        assert result.files[0].coverage_pct == 60.0


class TestRunBaselineScan:
    """Tests for ReviewOrchestrator.run_baseline_scan()."""

    @pytest.fixture
    def orchestrator(self, config: VibeHealConfig) -> ReviewOrchestrator:
        """Create ReviewOrchestrator with a mocked client and mocked git dependencies."""
        from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator

        mock_client = AsyncMock()
        return ReviewOrchestrator(config, mock_client, MagicMock(), MagicMock())

    @pytest.mark.asyncio
    async def test_success_returns_dashboard_url(self, orchestrator: ReviewOrchestrator) -> None:
        """On success, returns a BaselineScanResult with dashboard_url and no error."""
        with patch.object(
            orchestrator.analysis_runner,
            "run_analysis",
            new_callable=AsyncMock,
            return_value=AnalysisResult(success=True, dashboard_url="https://sonar.test.com/dashboard?id=my-project"),
        ) as mock_run_analysis:
            result = await orchestrator.run_baseline_scan()

        assert result.success is True
        assert result.project_key == "my-project"
        assert result.dashboard_url == "https://sonar.test.com/dashboard?id=my-project"
        assert result.error_message is None
        mock_run_analysis.assert_called_once_with(
            project_key="my-project",
            project_name="my-project",
            project_dir=Path.cwd(),
        )

    @pytest.mark.asyncio
    async def test_analysis_failure_returns_error(self, orchestrator: ReviewOrchestrator) -> None:
        """When analysis fails, returns a failed BaselineScanResult with the error message."""
        with patch.object(
            orchestrator.analysis_runner,
            "run_analysis",
            new_callable=AsyncMock,
            return_value=AnalysisResult(success=False, error_message="Scanner not found"),
        ):
            result = await orchestrator.run_baseline_scan()

        assert result.success is False
        assert result.project_key == "my-project"
        assert result.error_message == "Scanner not found"

    @pytest.mark.asyncio
    async def test_analysis_failure_with_no_message_uses_default(self, orchestrator: ReviewOrchestrator) -> None:
        """When analysis fails with no error_message, a default message is used."""
        with patch.object(
            orchestrator.analysis_runner,
            "run_analysis",
            new_callable=AsyncMock,
            return_value=AnalysisResult(success=False, error_message=None),
        ):
            result = await orchestrator.run_baseline_scan()

        assert result.success is False
        assert result.error_message == "Analysis failed"

    @pytest.mark.asyncio
    async def test_exception_is_caught_and_wrapped(self, orchestrator: ReviewOrchestrator) -> None:
        """An unexpected exception is caught and wrapped into a failed result."""
        with patch.object(
            orchestrator.analysis_runner,
            "run_analysis",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network down"),
        ):
            result = await orchestrator.run_baseline_scan()

        assert result.success is False
        assert result.project_key == "my-project"
        assert "network down" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_does_not_create_or_delete_temp_project(self, orchestrator: ReviewOrchestrator) -> None:
        """run_baseline_scan never creates or deletes a temp project."""
        with (
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                new_callable=AsyncMock,
                return_value=AnalysisResult(success=True),
            ),
            patch.object(orchestrator.project_manager, "create_temp_project", new_callable=AsyncMock) as mock_create,
            patch.object(orchestrator.project_manager, "delete_project", new_callable=AsyncMock) as mock_delete,
        ):
            await orchestrator.run_baseline_scan()

        mock_create.assert_not_called()
        mock_delete.assert_not_called()
